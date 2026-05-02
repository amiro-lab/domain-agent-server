"""데일리/위클리 리포트 생성."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from server.db import Memory, Report, Team, WeeklySchedule, engine

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

def domain_summary_from_memories(memories: list[Memory]) -> dict:
    groups = _group_by_tag(memories)
    result = {}
    for tag, mems in groups.items():
        result[tag] = [
            {"id": m.id, "type": m.mem_type, "description": m.description,
             "confidence": m.confidence, "platform": m.source_platform}
            for m in sorted(mems, key=lambda x: -x.confidence)
        ]
    return {"total": len(memories), "groups": result}


def domain_summary(session: Session, team_id: str) -> dict:
    memories = _all_memories(session, team_id)
    return domain_summary_from_memories(memories)


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

    domain_lines = [f"- {tag}: {len(mems)}개" for tag, mems in list(groups.items())[:10]]
    new_lines = "\n".join(f"- [{m.mem_type}] {m.description}" for m in new_mems) or "없음"
    prompt = (
        f"팀 위클리 도메인 리포트({period})를 작성해줘. 마크다운 사용.\n\n"
        f"## 이번 주 새로 학습된 지식 ({len(new_mems)}개)\n{new_lines}\n\n"
        f"## 현재 팀 도메인 지식 현황 (총 {len(all_mems)}개)\n" + "\n".join(domain_lines) + "\n\n"
        "위 내용을 바탕으로 팀의 이번 주 도메인 학습 현황, 주요 인사이트, 다음 주 주목할 점을 정리해줘."
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
    domain_lines = "\n".join(f"- {tag}: {len(items)}개" for tag, items in groups.items())

    prompt = (
        f"{member_name}의 지난 7일({period}) 업무 도메인 위클리를 작성해줘. 마크다운 사용.\n\n"
        f"## 수집된 지식 ({len(mems)}개)\n{mem_lines}\n\n"
        f"## 도메인 분포\n{domain_lines}\n\n"
        "위 내용을 바탕으로 이번 주 주요 업무·학습 내용, 핵심 인사이트, 다음 주 이어갈 것들을 자연스럽게 정리해줘. "
        "개인 위클리 회고 느낌으로 작성."
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
