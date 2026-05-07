"""데일리/위클리 리포트 생성."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from server.db import DomainSummary, Memory, Report, Team, WeeklySchedule, engine

DAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


def _memories_in_range(session: Session, team_id: str, start: datetime, end: datetime) -> list[Memory]:
    return (
        session.query(Memory)
        .filter(
            Memory.team_id == team_id,
            Memory.created_at >= start,
            Memory.created_at < end,
        )
        .all()
    )


def _all_memories(session: Session, team_id: str) -> list[Memory]:
    return session.query(Memory).filter_by(team_id=team_id).all()


def _group_by_tag(memories: list[Memory]) -> dict[str, list[Memory]]:
    groups: dict[str, list[Memory]] = {}
    for m in memories:
        tags = json.loads(m.tags) if m.tags else []
        ns_tags = [t for t in tags if ":" in t and not t.startswith("source:")]
        key = ns_tags[0] if ns_tags else "기타"
        groups.setdefault(key, []).append(m)
    return dict(sorted(groups.items()))


def _llm_summarize(prompt: str, operation: str = "report", ctx: dict | None = None) -> str:
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    try:
        if provider == "openai":
            import openai
            model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            resp = client.chat.completions.create(
                model=model,
                max_tokens=1000,
                messages=[
                    {"role": "system", "content": "팀 도메인 지식 리포트를 자연스러운 한국어로 작성하는 어시스턴트."},
                    {"role": "user", "content": prompt},
                ],
            )
            if ctx:
                from server import token_tracker
                usage = resp.usage
                token_tracker.record(
                    ctx.get("team_id", ""), ctx.get("team_name", ""), "",
                    "openai", model, operation,
                    usage.prompt_tokens, usage.completion_tokens,
                )
            return resp.choices[0].message.content.strip()
        else:
            import anthropic
            model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
            client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
            msg = client.messages.create(
                model=model,
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}],
            )
            if ctx:
                from server import token_tracker
                usage = msg.usage
                token_tracker.record(
                    ctx.get("team_id", ""), ctx.get("team_name", ""), "",
                    "anthropic", model, operation,
                    usage.input_tokens, usage.output_tokens,
                )
            return msg.content[0].text.strip()
    except Exception as e:
        return f"(리포트 생성 실패: {e})"


# ── 도메인 요약 ───────────────────────────────────────────

_TYPE_RANK = {"ontology": 0, "preference": 1, "fact": 2}


def _representative_sort_key(m: Memory):
    """도메인을 가장 잘 설명하는 메모리가 앞에 오도록 정렬.

    우선순위: ontology > preference > fact → source:declared 태그 → confidence 높음 → 최근 업데이트.
    """
    try:
        tag_list = json.loads(m.tags or "[]")
    except Exception:
        tag_list = []
    is_declared = "source:declared" in tag_list
    return (
        _TYPE_RANK.get(m.mem_type, 99),
        0 if is_declared else 1,
        -float(m.confidence or 0),
        -(m.updated_at.timestamp() if m.updated_at else 0),
    )


def domain_summary_from_memories(memories: list[Memory]) -> dict:
    groups = _group_by_tag(memories)
    result = {}
    for tag, mems in groups.items():
        result[tag] = [
            {"id": m.id, "type": m.mem_type, "description": m.description,
             "confidence": m.confidence, "platform": m.source_platform}
            for m in sorted(mems, key=_representative_sort_key)
        ]
    return {"total": len(memories), "groups": result}


def domain_summary(session: Session, team_id: str) -> dict:
    memories = _all_memories(session, team_id)
    return domain_summary_from_memories(memories)


# ── 도메인 한 줄 설명 (LLM 캐시) ──────────────────────────

def get_cached_domain_summaries(session: Session, team_id: str) -> dict[str, dict]:
    """팀의 캐시된 도메인 한 줄 설명. {tag: {summary, narrative, memory_count, updated_at, ...}}."""
    rows = session.query(DomainSummary).filter_by(team_id=team_id).all()
    return {
        r.tag: {
            "summary": r.summary,
            "narrative": r.narrative or "",
            "memory_count": r.memory_count,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            "narrative_updated_at": (
                r.narrative_updated_at.isoformat() if r.narrative_updated_at else None
            ),
        }
        for r in rows
    }


def rebuild_domain_summaries(
    session: Session, team_id: str, top_n: int = 30, min_count: int = 1,
) -> dict:
    """팀의 도메인 태그 상위 top_n개에 대해 LLM이 한 줄 설명 생성·캐시.

    각 도메인의 대표 메모리(_representative_sort_key 우선)들을 보고 '이 도메인은 X' 형태로 1문장 작성.
    archived 메모리는 합성 입력에서 제외 (사라질 운명의 메모리가 도메인 정의에 들어가면 안 됨).
    """
    memories = [m for m in _all_memories(session, team_id) if m.archived_at is None]
    groups = _group_by_tag(memories)

    targets = sorted(
        [(tag, mems) for tag, mems in groups.items() if len(mems) >= min_count],
        key=lambda x: -len(x[1]),
    )[:top_n]

    if not targets:
        return {"updated": 0, "skipped": 0}

    blocks = []
    for tag, mems in targets:
        repr_mems = sorted(mems, key=_representative_sort_key)[:6]
        descs = "\n".join(f"- [{m.mem_type}] {m.description}" for m in repr_mems)
        blocks.append(f"## `{tag}` ({len(mems)}개 메모리)\n{descs}")
    prompt_body = "\n\n".join(blocks)

    prompt = (
        "각 도메인 태그가 무엇에 관한 도메인인지 한 줄로 설명해줘.\n"
        "형식: 각 줄에 정확히 한 태그씩, `{태그}: {한 줄 설명}` 형태로.\n"
        "한 줄 설명은 자연스러운 한국어로, '이 도메인은/이 프로젝트는 X를 하는 ...' 같이 도메인의 본질·목적·주제가 드러나도록.\n"
        "예시:\n"
        "- `domain:domain-agent`: 사용자의 도메인을 기억하고 이해하는 AI 에이전트를 만드는 프로젝트\n"
        "- `system:codesign`: macOS 앱 코드 서명·재서명 관련 작업 도메인\n"
        "주의: 단일 메모리를 그대로 인용하지 말고 여러 메모리를 종합해 도메인의 의미를 한 줄로 압축. AI 평가·감상('인상적', '~로 보입니다')·사담 금지.\n"
        f"마크다운 글머리표 없이, 한 줄당 정확히 `태그: 설명` 형식만.\n\n"
        "---\n\n"
        f"{prompt_body}"
    )
    team = session.query(Team).filter_by(id=team_id).first()
    ctx = {"team_id": team_id, "team_name": team.name if team else ""}
    raw = _llm_summarize(prompt, "domain_describe", ctx)

    import re
    target_tags = {tag for tag, _ in targets}
    parsed: dict[str, str] = {}
    pattern = re.compile(r"^\s*[-*]?\s*`([^`]+)`\s*[:：]\s*(.+?)\s*$")
    for line in raw.splitlines():
        m = pattern.match(line)
        if m:
            tag, body = m.group(1).strip(), m.group(2).strip()
            if tag and body:
                parsed[tag] = body
            continue
        line_clean = line.strip().lstrip("-*•").strip()
        for tag in target_tags:
            if line_clean.startswith(tag + ":") or line_clean.startswith(tag + " :") or line_clean.startswith(tag + "："):
                body = line_clean[len(tag):].lstrip(":：").strip()
                if body:
                    parsed[tag] = body
                break

    updated = 0
    for tag, mems in targets:
        summary = parsed.get(tag)
        if not summary:
            continue
        row = session.query(DomainSummary).filter_by(team_id=team_id, tag=tag).first()
        if row:
            row.summary = summary
            row.memory_count = len(mems)
        else:
            session.add(DomainSummary(team_id=team_id, tag=tag, summary=summary, memory_count=len(mems)))
        updated += 1
    session.commit()

    return {"updated": updated, "skipped": len(targets) - updated, "total_targets": len(targets)}


def rebuild_domain_narratives(
    session: Session, team_id: str, top_n: int = 8, min_count: int = 5,
) -> dict:
    """도메인별 multi-paragraph narrative 생성·캐시.

    한 줄 요약(rebuild_domain_summaries)을 보완. 시간순으로 정렬한 대표 메모리 ~20개를
    LLM에 주고 4개 섹션 narrative 생성:
      ## 배경 / ## 결정의 흐름 / ## 현재 / ## 미해결

    top_n 기본 8 (비용 큼 — 한 줄 요약 30개와 별도 운영). min_count 기본 5
    (메모리 적은 도메인은 narrative 합성해도 빈약).
    """
    from datetime import datetime as _dt, timezone as _tz
    memories = [m for m in _all_memories(session, team_id) if m.archived_at is None]
    groups = _group_by_tag(memories)
    targets = sorted(
        [(tag, mems) for tag, mems in groups.items() if len(mems) >= min_count],
        key=lambda x: -len(x[1]),
    )[:top_n]
    if not targets:
        return {"updated": 0, "skipped": 0, "total_targets": 0}

    team = session.query(Team).filter_by(id=team_id).first()
    ctx = {"team_id": team_id, "team_name": team.name if team else ""}
    now = _dt.now(_tz.utc)
    epoch_min = _dt.min.replace(tzinfo=_tz.utc)

    updated = 0
    for tag, mems in targets:
        # 시간순(asc) — 진화 흐름 파악용
        time_sorted = sorted(mems, key=lambda m: m.created_at or epoch_min)
        # 대표 ~20개: 시간 양 끝 + 신뢰도 높은 것 mix.
        # 단순화: 시간 sample + representative_sort_key 상위 합쳐서 dedup, 20개 cap.
        repr_pool = sorted(mems, key=_representative_sort_key)[:30]
        seen, picked = set(), []
        for m in time_sorted + repr_pool:
            if m.id in seen:
                continue
            seen.add(m.id)
            picked.append(m)
            if len(picked) >= 20:
                break
        # 시간순으로 다시 정렬해서 narrative에 일관된 순서 제공
        picked.sort(key=lambda m: m.created_at or epoch_min)

        block_lines = []
        for m in picked:
            d = (m.created_at or epoch_min).date().isoformat()
            content_excerpt = (m.content or "").replace("\n", " ")[:300]
            block_lines.append(f"- [{m.mem_type}] ({d}) {m.description}\n    {content_excerpt}")
        block = "\n".join(block_lines)

        prompt = (
            f"다음은 '{tag}' 도메인의 메모리 {len(picked)}개 (시간순 오래된 → 최근).\n"
            "이 도메인이 어떻게 진화했는지 아래 4개 섹션 narrative로 작성하라:\n\n"
            "## 배경\n"
            "이 도메인이 왜 시작됐는가 — 풀려는 문제·전제·제약. 1~2문단.\n\n"
            "## 결정의 흐름\n"
            "어떤 시도가 있었고 무엇이 채택·폐기됐는지 시간순으로 1~3문단. "
            "메모리들 사이의 인과 관계가 보이면 명시 (예: 'X 발견 → Y 시도 → Z 안착').\n\n"
            "## 현재\n"
            "지금의 합의된 상태·접근. 1~2문단.\n\n"
            "## 미해결\n"
            "아직 풀리지 않은 과제·관찰된 부작용·미상 사항. 1문단. "
            "정보 부족하면 '미상' 한 줄로.\n\n"
            "규칙:\n"
            "- 한국어로 작성. 전문 용어·코드·고유명사·수치는 영어/원문 인용 OK\n"
            "- 단순 메모리 인용 금지 — 종합적 narrative\n"
            "- 메모리에 없는 정보 추측 금지\n"
            "- 마크다운 헤더 형식 정확히 (## 배경, ## 결정의 흐름, ## 현재, ## 미해결)\n\n"
            f"메모리:\n{block}"
        )
        narrative = _llm_summarize(prompt, "domain_narrative", ctx)
        if not narrative or narrative.startswith("(리포트 생성 실패"):
            continue
        row = session.query(DomainSummary).filter_by(team_id=team_id, tag=tag).first()
        if row:
            row.narrative = narrative
            row.narrative_updated_at = now
            row.memory_count = len(mems)
        else:
            row = DomainSummary(
                team_id=team_id, tag=tag, summary="",
                narrative=narrative, memory_count=len(mems),
                narrative_updated_at=now,
            )
            session.add(row)
        updated += 1

    session.commit()
    return {"updated": updated, "skipped": len(targets) - updated, "total_targets": len(targets)}


def rebuild_all_team_summaries(top_n: int = 30, min_count: int = 2) -> None:
    """모든 활성 팀에 대해 도메인 요약 자동 재생성. weekly cron용.

    min_count=2: 메모리 1개뿐인 도메인은 합성 의미 없고 LLM 비용만 듦.
    """
    import logging
    log = logging.getLogger("janitor")  # janitor 로그에 함께 누적
    with Session(engine) as session:
        teams = session.query(Team).all()
        for team in teams:
            try:
                result = rebuild_domain_summaries(session, team.id, top_n=top_n, min_count=min_count)
                log.info(f"[domain_summary] team={team.id} {result}")
            except Exception as e:
                log.error(f"[domain_summary] team={team.id} ERROR: {e}")


# ── 데일리 리포트 ─────────────────────────────────────────

def generate_daily(session: Session, team_id: str, target_date: date | None = None) -> dict:
    if target_date is None:
        target_date = date.today()

    period = target_date.isoformat()
    existing = session.query(Report).filter_by(team_id=team_id, report_type="daily", period=period).first()
    if existing:
        return {"period": period, "content": existing.content, "new_count": existing.new_memory_count, "cached": True}

    start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    new_mems = _memories_in_range(session, team_id, start, end)

    team = session.query(Team).filter_by(id=team_id).first()
    ctx = {"team_id": team_id, "team_name": team.name if team else ""}

    if not new_mems:
        content = f"**{period} 데일리 리포트**\n\n오늘 새로 학습된 도메인 지식이 없습니다."
    else:
        facts = "\n".join(f"- [{m.mem_type}] {m.description}" for m in new_mems)
        prompt = (
            f"오늘({period}) 팀이 AI 대화에서 새로 학습한 도메인 지식 {len(new_mems)}개를 "
            f"데일리 리포트 형식으로 정리해줘. 마크다운 사용.\n\n{facts}"
        )
        content = _llm_summarize(prompt, "report_daily", ctx)

    report = Report(
        team_id=team_id, report_type="daily", period=period,
        content=content, new_memory_count=len(new_mems),
    )
    session.add(report)
    session.commit()
    return {"period": period, "content": content, "new_count": len(new_mems), "cached": False}


# ── 위클리 리포트 ─────────────────────────────────────────

def _week_label(d: date) -> str:
    return f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"


def generate_weekly(session: Session, team_id: str, week_label: str | None = None) -> dict:
    today = date.today()
    if week_label is None:
        monday = today - timedelta(days=today.weekday() + 7)
        week_label = _week_label(monday)
    else:
        monday = date.fromisocalendar(*[int(x) for x in week_label.replace("W", "").split("-")], 1) \
            if "-W" in week_label else today - timedelta(days=today.weekday() + 7)

    period = week_label
    existing = session.query(Report).filter_by(team_id=team_id, report_type="weekly", period=period).first()
    if existing:
        return {"period": period, "content": existing.content, "new_count": existing.new_memory_count, "cached": True}

    start = datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    new_mems = _memories_in_range(session, team_id, start, end)
    all_mems = _all_memories(session, team_id)
    groups = _group_by_tag(all_mems)

    team = session.query(Team).filter_by(id=team_id).first()
    ctx = {"team_id": team_id, "team_name": team.name if team else ""}

    domain_lines = []
    for tag, mems in list(groups.items())[:10]:
        sorted_mems = sorted(mems, key=lambda m: m.confidence, reverse=True)[:3]
        samples = "\n".join(f"    · {m.description}" for m in sorted_mems)
        domain_lines.append(f"- {tag}: {len(mems)}개\n{samples}")
    new_lines = "\n".join(f"- [{m.mem_type}] {m.description}" for m in new_mems) or "없음"
    prompt = (
        f"팀 위클리 도메인 리포트({period})를 작성해줘. 마크다운 사용.\n\n"
        f"## 이번 주 새로 학습된 지식 ({len(new_mems)}개)\n{new_lines}\n\n"
        f"## 현재 팀 도메인 지식 현황 (총 {len(all_mems)}개)\n" + "\n".join(domain_lines) + "\n\n"
        "위 내용을 바탕으로 다음 5개 섹션으로 정리해줘:\n"
        "1) 이번 주 주요 업무 내용 — 실제 반영·완료·적용된 작업 위주 (배포·머지·구현 완료·문서 확정·결정 채택 등). 단순 논의·아이디어·검토 단계는 제외하고, 산출물이 실제로 적용된 항목만 작성.\n"
        "2) 이번 주 주요 학습 내용 (새로 알게 된 사실·개념·도메인 지식)\n"
        "3) 주요 인사이트\n"
        "4) 다음 주 주목할 점\n"
        "5) 도메인 한 줄 요약 — 위 '현재 팀 도메인 지식 현황'에 나온 각 도메인 태그가 무엇에 관한 것인지 그 태그 아래 대표 항목을 보고 한 줄로 설명. 형식: `- \\`{tag}\\`: {한 줄 요약} ({N}개)`.\n"
        "업무 내용과 학습 내용은 반드시 별도 섹션으로 분리할 것 (합치지 마라).\n"
        "주의: 위에 나열된 메모리 내용에서 직접 도출되는 사실만 적어라. "
        "AI의 주관적 평가·감상·권고("
        "'~로 보입니다', '인상적입니다', '다행히', '바람직합니다', '~하는 것이 좋겠습니다' 등)는 절대 넣지 마라. "
        "각 섹션은 메모리에 등장한 항목을 그대로 인용하거나 요약하는 식으로만 작성."
    )
    content = _llm_summarize(prompt, "report_weekly", ctx)

    report = Report(
        team_id=team_id, report_type="weekly", period=period,
        content=content, new_memory_count=len(new_mems),
    )
    session.add(report)
    session.commit()
    return {"period": period, "content": content, "new_count": len(new_mems), "cached": False}


def generate_member_weekly(session: Session, team_id: str, member_name: str) -> dict:
    """오늘 기준 7일치 내 메모리를 기반으로 개인 위클리 리포트 생성."""
    today = date.today()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=7)
    period = f"{start.strftime('%Y.%m.%d')} ~ {today.strftime('%Y.%m.%d')}"

    mems = (
        session.query(Memory)
        .filter(
            Memory.team_id == team_id,
            Memory.captured_by == member_name,
            Memory.created_at >= start,
            Memory.created_at < end,
        )
        .order_by(Memory.created_at.asc())
        .all()
    )

    if not mems:
        return {"period": period, "content": "최근 7일간 수집된 도메인 지식이 없습니다.", "count": 0}

    groups = _group_by_tag(mems)
    mem_lines = "\n".join(f"- [{m.mem_type}] {m.description}" for m in mems)
    domain_blocks = []
    for tag, items in groups.items():
        sorted_items = sorted(items, key=lambda m: m.confidence, reverse=True)[:3]
        samples = "\n".join(f"    · {m.description}" for m in sorted_items)
        domain_blocks.append(f"- {tag}: {len(items)}개\n{samples}")
    domain_lines = "\n".join(domain_blocks)

    prompt = (
        f"{member_name}의 지난 7일({period}) 업무 도메인 위클리를 작성해줘. 마크다운 사용.\n\n"
        f"## 수집된 지식 ({len(mems)}개)\n{mem_lines}\n\n"
        f"## 도메인 분포\n{domain_lines}\n\n"
        "위 내용을 바탕으로 다음 5개 섹션으로 정리해줘:\n"
        "1) 이번 주 주요 업무 내용 — 실제 반영·완료·적용된 작업 위주 (배포·머지·구현 완료·문서 확정·결정 채택 등). 단순 논의·아이디어·검토 단계는 제외하고, 산출물이 실제로 적용된 항목만 작성.\n"
        "2) 이번 주 주요 학습 내용 (새로 알게 된 사실·개념·도메인 지식)\n"
        "3) 핵심 인사이트\n"
        "4) 다음 주 이어갈 것들\n"
        "5) 도메인 한 줄 요약 — 위 '도메인 분포'에 나온 각 도메인 태그가 무엇에 관한 것인지 그 태그 아래 대표 항목을 보고 한 줄로 설명. 형식: `- \\`{tag}\\`: {한 줄 요약} ({N}개)`.\n"
        "업무 내용과 학습 내용은 반드시 별도 섹션으로 분리할 것 (합치지 마라).\n"
        "주의: 위에 나열된 메모리 내용에서 직접 도출되는 사실만 적어라. "
        "AI의 주관적 평가·감상·권고("
        "'~로 보입니다', '인상적입니다', '다행히', '바람직합니다', '~하는 것이 좋겠습니다', '훌륭한 한 주였습니다' 등)는 절대 넣지 마라. "
        "각 섹션은 메모리에 등장한 항목을 그대로 인용하거나 요약하는 식으로만 작성."
    )
    ctx = {"team_id": team_id, "member_name": member_name}
    content = _llm_summarize(prompt, "report_weekly", ctx)
    return {"period": period, "content": content, "count": len(mems)}


def generate_weekly_all_teams():
    with Session(engine) as session:
        teams = session.query(Team).filter_by(enabled=True).all()
        for team in teams:
            sched = session.query(WeeklySchedule).filter_by(team_id=team.id, enabled=True).first()
            if sched:
                try:
                    generate_weekly(session, team.id)
                    print(f"[weekly] {team.name} 위클리 생성 완료")
                except Exception as e:
                    print(f"[weekly] {team.name} 실패: {e}")
