"""팀 메모리 CRUD."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from server.db import Memory

# fuzzy merge 임계: description 토큰 Jaccard ≥ 이 값이면 같은 항목으로 본다.
FUZZY_MERGE_THRESHOLD = 0.6
CONFIDENCE_CAP = 0.95

# capture 게이트 — LLM이 뽑은 항목이 메모리에 들어갈 자격이 있는지 결정.
# 노이즈 폭증을 막기 위한 입구 통제. 값은 하드코딩 — 너무 자주 바뀌면 의미 없음.
CAPTURE_MIN_CONFIDENCE = 0.60
CAPTURE_MIN_DESC_LEN = 15
CAPTURE_MIN_CONTENT_LEN = 30
_VALID_TYPES = ("fact", "preference", "ontology")

# 자동 머지 제외 — 버전 식별자(v3_fixed, v4_fixed, …)는 의미 보존이 우선.
_PROTECTED_TAG_RE = re.compile(r"^v\d+_fixed$")
_TOKEN_RE = re.compile(r"[\w가-힣]+", re.UNICODE)


def _iso_utc(dt: datetime | None) -> str:
    """SQLite는 timezone을 보존하지 않아 naive로 읽힘. UTC로 명시해 ISO 직렬화."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _desc_hash(description: str) -> str:
    return hashlib.sha256(description.strip().lower().encode()).hexdigest()[:16]


def _make_id(mem_type: str, description: str) -> str:
    today = date.today().isoformat().replace("-", "")
    h = _desc_hash(description)
    slug = description.lower().replace(" ", "-")[:30]
    return f"{mem_type}_{today}_{slug}_{h}"


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) >= 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def has_protected_tag(tags: list[str]) -> bool:
    return any(_PROTECTED_TAG_RE.match(t or "") for t in tags)


def normalize_tag(tag: str) -> str:
    """단일 태그를 canonical 형식으로 정규화.

    규칙:
      - 앞뒤 공백 제거
      - PROTECTED 태그(v\\d+_fixed)는 그대로 보존 (값 안 건드림)
      - prefix(콜론 앞)는 소문자
      - value(콜론 뒤)는 소문자 + `_` → `-` 치환
      - 빈 문자열 / 공백만은 그대로 반환 (호출자가 필터링)
    """
    if tag is None:
        return ""
    s = tag.strip()
    if not s:
        return s
    if _PROTECTED_TAG_RE.match(s):
        return s
    if ":" in s:
        prefix, _, value = s.partition(":")
        prefix = prefix.lower().strip()
        value = value.strip().lower().replace("_", "-")
        return f"{prefix}:{value}" if value else prefix
    return s.lower().replace("_", "-")


def normalize_tags(tags: list[str]) -> list[str]:
    """태그 리스트 정규화 + 중복 제거 (순서 유지)."""
    out: list[str] = []
    for t in tags or []:
        nt = normalize_tag(t)
        if nt and nt not in out:
            out.append(nt)
    return out


def _parse_tags(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return v if isinstance(v, list) else []


def _union_tags(*tag_lists: list[str]) -> list[str]:
    out: list[str] = []
    for tl in tag_lists:
        for t in tl or []:
            if t and t not in out:
                out.append(t)
    return out


def find_similar(
    session: Session,
    team_id: str,
    mem_type: str,
    description: str,
    *,
    threshold: float = FUZZY_MERGE_THRESHOLD,
) -> Memory | None:
    """동일 team_id + mem_type + active 안에서 description Jaccard ≥ threshold 항목 반환.

    PROTECTED 태그(v*_fixed)가 있는 후보는 의미 보존을 위해 제외 (버전 식별자라 머지 불가).
    """
    desc_tokens = _tokens(description)
    if not desc_tokens:
        return None
    candidates = (
        session.query(Memory)
        .filter_by(team_id=team_id, mem_type=mem_type)
        .filter(Memory.archived_at.is_(None))
        .all()
    )
    best: Memory | None = None
    best_score = 0.0
    for m in candidates:
        if has_protected_tag(_parse_tags(m.tags)):
            continue
        score = _jaccard(desc_tokens, _tokens(m.description))
        if score > best_score:
            best = m
            best_score = score
    return best if best_score >= threshold else None


def save(session: Session, team_id: str, mem_type: str, description: str,
         content: str, confidence: float, tags: list[str],
         platform: str = "", captured_by: str = "", session_id: str = "") -> Memory:
    """upsert + fuzzy merge.

    1) description_hash 정확 일치 → 기존 항목 갱신 (콘텐츠/태그 교체, last_verified 갱신)
    2) (1) 없을 때: 들어온 항목에 protected tag 없으면 Jaccard로 유사 항목 탐색 → bump
       - confidence = min(0.95, max(기존, 신규))
       - tags = union
       - content/description은 기존 유지 (canonical)
       - last_verified_at = now, archived_at = None
       - session_id는 빈 경우만 새 값으로 채움 (기존 attribution 보존)
    3) 둘 다 미스 → 신규 insert (session_id 포함)
    """
    d_hash = _desc_hash(description)
    now = datetime.now(timezone.utc)
    # 입력 단계에서 태그 정규화: V5→v5, batch_api→batch-api, PROTECTED 보존.
    tags = normalize_tags(tags)

    existing = session.query(Memory).filter_by(team_id=team_id, description_hash=d_hash).first()
    if existing:
        existing.content = content
        existing.confidence = confidence
        existing.tags = json.dumps(tags, ensure_ascii=False)
        existing.source_platform = platform
        if captured_by:
            existing.captured_by = captured_by
        if session_id and not existing.session_id:
            existing.session_id = session_id
        existing.last_verified_at = now
        existing.archived_at = None
        session.commit()
        return existing

    if not has_protected_tag(tags):
        similar = find_similar(session, team_id, mem_type, description)
        if similar is not None:
            similar.confidence = min(CONFIDENCE_CAP, max(similar.confidence, confidence))
            similar.tags = json.dumps(
                _union_tags(_parse_tags(similar.tags), tags), ensure_ascii=False
            )
            similar.last_verified_at = now
            similar.archived_at = None
            if captured_by and not similar.captured_by:
                similar.captured_by = captured_by
            if session_id and not similar.session_id:
                similar.session_id = session_id
            session.commit()
            return similar

    mem = Memory(
        id=_make_id(mem_type, description),
        team_id=team_id,
        mem_type=mem_type,
        description=description,
        description_hash=d_hash,
        content=content,
        confidence=confidence,
        tags=json.dumps(tags, ensure_ascii=False),
        source_platform=platform,
        captured_by=captured_by,
        session_id=session_id,
        last_verified_at=now,
    )
    session.add(mem)
    session.commit()
    return mem


def query(session: Session, team_id: str, q: str, limit: int = 10) -> list[Memory]:
    all_mems = (session.query(Memory)
                .filter_by(team_id=team_id)
                .filter(Memory.archived_at.is_(None))
                .all())
    if not q:
        return all_mems[:limit]
    q_lower = q.lower()
    results = [
        m for m in all_mems
        if q_lower in m.description.lower() or q_lower in m.content.lower() or q_lower in m.tags.lower()
    ]
    return results[:limit]


def list_all(session: Session, team_id: str, mem_type: str | None = None,
             tags: list[str] | None = None) -> list[Memory]:
    q = session.query(Memory).filter_by(team_id=team_id).filter(Memory.archived_at.is_(None))
    if mem_type:
        q = q.filter_by(mem_type=mem_type)
    mems = q.all()
    if tags:
        mems = [m for m in mems if any(t in m.tags for t in tags)]
    return mems


def delete(session: Session, team_id: str, memory_id: str) -> bool:
    mem = session.query(Memory).filter_by(id=memory_id, team_id=team_id).first()
    if not mem:
        return False
    session.delete(mem)
    session.commit()
    return True


def should_capture(item: dict) -> tuple[bool, str]:
    """LLM 추출 항목이 메모리 저장 자격이 있는지 검사.

    반환: (accept: bool, reason: str). reason은 reject 사유 또는 'ok'.
    호출자는 reject 시 reason을 카운터/로그에 누적해 capture 품질 모니터링.
    """
    mem_type = item.get("type")
    if mem_type not in _VALID_TYPES:
        return False, f"invalid_type:{mem_type}"

    description = (item.get("description") or "").strip()
    if len(description) < CAPTURE_MIN_DESC_LEN:
        return False, f"desc_too_short:{len(description)}"

    content = (item.get("content") or "").strip()
    if len(content) < CAPTURE_MIN_CONTENT_LEN:
        return False, f"content_too_short:{len(content)}"

    try:
        confidence = float(item.get("confidence", 0.7))
    except (TypeError, ValueError):
        return False, "invalid_confidence"
    if confidence < CAPTURE_MIN_CONFIDENCE:
        return False, f"low_confidence:{confidence:.2f}"

    # description이 알파/한글 1글자도 없으면 노이즈 (숫자·기호만)
    toks = _tokens(description)
    if not toks or not any(any(not c.isdigit() for c in t) for t in toks):
        return False, "no_meaningful_tokens"

    return True, "ok"


def filter_capture_items(items: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """should_capture로 일괄 필터링. (kept_items, reject_reason_counts) 반환."""
    kept: list[dict] = []
    reasons: dict[str, int] = {}
    for it in items or []:
        ok, reason = should_capture(it)
        if ok:
            kept.append(it)
        else:
            reasons[reason] = reasons.get(reason, 0) + 1
    return kept, reasons


# namespace consolidation — 같은 value를 prefix 다르게 박아 분산된 태그를 하나로 합침.
# 예: project:a2a-ctgr-match (34건) + a2a-ctgr-match (27건) + domain:a2a-ctgr-match (5건)
# → 가장 빈도 높은 project:a2a-ctgr-match로 통합.
# 동률 시 prefix 우선순위로 tiebreak.
_PREFIX_PRIORITY = ("project", "domain", "team", "system", "tech", "person")


def _tag_value(tag: str) -> str:
    """태그에서 value 부분(콜론 뒤). 콜론 없으면 태그 자체. PROTECTED는 별도 처리됨."""
    if ":" in tag:
        return tag.partition(":")[2]
    return tag


def _tag_prefix(tag: str) -> str:
    """태그 prefix(콜론 앞). 콜론 없으면 빈 문자열 (flat 태그 표시)."""
    return tag.partition(":")[0] if ":" in tag else ""


def _canonical_pick_key(tag: str, count: int) -> tuple:
    """sort key — count desc → prefix priority asc → 알파 asc.

    tuple로 반환: (-count, prefix_rank, tag). max key가 canonical.
    실제 사용은 min()이라 (count는 음수, rank는 양수).
    """
    prefix = _tag_prefix(tag)
    rank = _PREFIX_PRIORITY.index(prefix) if prefix in _PREFIX_PRIORITY else len(_PREFIX_PRIORITY)
    return (-count, rank, tag)


def build_namespace_canonical_map(memories: list[Memory]) -> dict[str, str]:
    """팀 메모리 전체에서 namespace 분산을 분석해 {old_tag: canonical_tag} 매핑 생성.

    규칙:
      - source:* 는 매핑에서 제외 (메타 태그)
      - PROTECTED 태그(v\\d+_fixed)는 제외 (의미 보존)
      - 같은 value를 가진 변종 2개 이상이면 클러스터로 묶음
      - canonical: count 최대 → 동률시 _PREFIX_PRIORITY 우선 → 동률시 알파순
      - 변종 모두 같은 prefix면 매핑 안 만듦 (할 일 없음)
    """
    from collections import Counter

    tag_counts: Counter[str] = Counter()
    for m in memories:
        for t in _parse_tags(m.tags):
            if not t or t.startswith("source:") or _PROTECTED_TAG_RE.match(t):
                continue
            tag_counts[t] += 1

    by_value: dict[str, list[str]] = {}
    for tag in tag_counts:
        by_value.setdefault(_tag_value(tag), []).append(tag)

    mapping: dict[str, str] = {}
    for value, variants in by_value.items():
        if len(variants) < 2:
            continue
        # 모두 같은 prefix면 합칠 게 없음
        if len({_tag_prefix(v) for v in variants}) == 1:
            continue
        canonical = min(variants, key=lambda t: _canonical_pick_key(t, tag_counts[t]))
        for v in variants:
            if v != canonical:
                mapping[v] = canonical
    return mapping


def consolidate_namespaces(
    session: Session,
    team_id: str,
    *,
    dry_run: bool = True,
) -> dict:
    """팀 메모리의 namespace 분산을 일괄 통합 (project:X / X / domain:X → 가장 빈번한 변종).

    PROTECTED 태그(v*_fixed)와 source:* 는 손대지 않음.
    반환: {dry_run, team_id, scanned, changed_memories, clusters, mapping_samples}
    """
    mems = (
        session.query(Memory)
        .filter_by(team_id=team_id)
        .filter(Memory.archived_at.is_(None))
        .all()
    )
    mapping = build_namespace_canonical_map(mems)
    if not mapping:
        return {
            "dry_run": dry_run,
            "team_id": team_id,
            "scanned": len(mems),
            "changed_memories": 0,
            "clusters": 0,
            "mapping_samples": [],
        }

    # mapping을 canonical 기준 클러스터로 재구성 (UI 표시용)
    clusters: dict[str, list[tuple[str, int]]] = {}
    cluster_counts: dict[str, int] = {}
    for mem in mems:
        for t in _parse_tags(mem.tags):
            cluster_counts[t] = cluster_counts.get(t, 0) + 1
    for old, canonical in mapping.items():
        clusters.setdefault(canonical, []).append((old, cluster_counts.get(old, 0)))

    changed_memories = 0
    for m in mems:
        original = _parse_tags(m.tags)
        replaced = [mapping.get(t, t) for t in original]
        # dedup (변종 통합 후 같은 태그가 두 번 나올 수 있음)
        deduped: list[str] = []
        for t in replaced:
            if t and t not in deduped:
                deduped.append(t)
        if deduped != original:
            changed_memories += 1
            if not dry_run:
                m.tags = json.dumps(deduped, ensure_ascii=False)

    if not dry_run:
        session.commit()

    samples = [
        {
            "canonical": canonical,
            "canonical_count": cluster_counts.get(canonical, 0),
            "merged_from": [{"tag": old, "count": cnt} for old, cnt in items],
        }
        for canonical, items in sorted(
            clusters.items(),
            key=lambda kv: -cluster_counts.get(kv[0], 0),
        )[:30]
    ]

    return {
        "dry_run": dry_run,
        "team_id": team_id,
        "scanned": len(mems),
        "changed_memories": changed_memories,
        "clusters": len(clusters),
        "mapping_samples": samples,
    }


def _has_korean(text: str | None) -> bool:
    """가-힣 범위 글자가 하나라도 있으면 True."""
    if not text:
        return False
    return any("가" <= c <= "힣" for c in text)


def _looks_english(description: str | None, content: str | None) -> bool:
    """description+content에 한글 0개 + 라틴 알파벳 30+자면 영어로 판정.

    drunk-bar 같은 멀티에이전트 transcript처럼 한글이 전혀 안 섞인 경우만
    번역 대상. 한국어 본문에 영어 코드/용어 섞인 일반 메모리는 통과시킴.
    """
    if _has_korean(description) or _has_korean(content):
        return False
    blob = f"{description or ''} {content or ''}"
    latin = sum(1 for c in blob if c.isalpha() and ord(c) < 128)
    return latin >= 30


def find_english_memories(
    session: Session, team_id: str, *, limit: int = 200,
) -> list[Memory]:
    """팀 active 메모리 중 영어로 판정된 것 (limit개 까지)."""
    mems = (
        session.query(Memory)
        .filter_by(team_id=team_id)
        .filter(Memory.archived_at.is_(None))
        .all()
    )
    return [m for m in mems if _looks_english(m.description, m.content)][:limit]


def translate_english_memories(
    session: Session, team_id: str, *, dry_run: bool = True, limit: int = 50,
) -> dict:
    """영어 description/content를 가진 active 메모리를 한국어로 번역.

    동작:
      1) _looks_english 통과 항목 수집 (최대 limit개)
      2) dry_run=True: 번역 안 하고 후보 카운트 + 샘플만 반환 (LLM 비용 0)
      3) dry_run=False: 10개씩 배치로 LLM(Haiku)에 번역 요청 → 결과로 description/content 교체
         description_hash도 갱신. 번역 후 hash가 다른 메모리와 충돌하면 그 항목 스킵.

    PROTECTED 태그는 번역 대상에서 제외 (의미 보존).
    """
    candidates = find_english_memories(session, team_id, limit=limit)
    # PROTECTED는 의미 보존을 위해 번역 안 함
    targets = [m for m in candidates if not has_protected_tag(_parse_tags(m.tags))]

    if not targets:
        return {
            "dry_run": dry_run, "team_id": team_id,
            "candidates": 0, "translated": 0, "samples": [],
        }

    if dry_run:
        return {
            "dry_run": True, "team_id": team_id,
            "candidates": len(targets),
            "translated": 0,
            "samples": [
                {
                    "id": m.id,
                    "description": m.description,
                    "content_preview": (m.content or "")[:200],
                }
                for m in targets[:20]
            ],
        }

    from server.reporter import _llm_summarize

    translated_count = 0
    samples: list[dict] = []
    skipped_conflict = 0

    for batch_start in range(0, len(targets), 10):
        batch = targets[batch_start:batch_start + 10]
        payload = [
            {"id": m.id, "description": m.description, "content": m.content}
            for m in batch
        ]
        prompt = (
            "다음 메모리 항목들의 description과 content를 한국어로 번역하라.\n"
            "전문 용어·고유명사·코드·라이브러리/툴 이름·파일명·CLI 명령·에러 메시지는 영어 원문 인용 유지.\n"
            "문장 골격(주어/서술어/접속어)은 한국어로.\n"
            "응답은 단일 JSON 객체. 마크다운 코드블록 금지:\n"
            '{"translated": [{"id":"...", "description":"...", "content":"..."}, ...]}\n'
            "id는 입력과 정확히 동일하게 유지. content가 길면 의미 유지하면서 한국어로 자연스럽게 다시 써라.\n\n"
            "입력:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
        )
        raw = _llm_summarize(prompt, "translate_to_korean", {"team_id": team_id, "team_name": ""})
        try:
            s = raw.strip()
            if s.startswith("```"):
                s = s.split("\n", 1)[1] if "\n" in s else ""
                s = s.rsplit("```", 1)[0].strip()
            if "{" in s and "}" in s:
                s = s[s.index("{"):s.rindex("}") + 1]
            parsed = json.loads(s).get("translated", [])
        except (json.JSONDecodeError, ValueError, AttributeError):
            continue

        by_id = {m.id: m for m in batch}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            mid = item.get("id")
            mem = by_id.get(mid)
            if not mem:
                continue
            new_desc = (item.get("description") or "").strip()
            new_content = (item.get("content") or "").strip()
            if not new_desc or not new_content:
                continue
            new_hash = _desc_hash(new_desc)
            # description_hash 충돌 검사 — 번역 후 다른 메모리와 같아지면 스킵
            if new_hash != mem.description_hash:
                conflict = (
                    session.query(Memory)
                    .filter_by(team_id=team_id, description_hash=new_hash)
                    .first()
                )
                if conflict and conflict.id != mem.id:
                    skipped_conflict += 1
                    continue

            old_desc = mem.description
            mem.description = new_desc
            mem.content = new_content
            mem.description_hash = new_hash
            translated_count += 1
            if len(samples) < 20:
                samples.append({
                    "id": mid, "before": old_desc, "after": new_desc,
                })

    session.commit()

    return {
        "dry_run": False, "team_id": team_id,
        "candidates": len(targets),
        "translated": translated_count,
        "skipped_conflict": skipped_conflict,
        "samples": samples,
    }


def normalize_existing_tags(session: Session, team_id: str, *, dry_run: bool = True) -> dict:
    """팀의 active 메모리에서 정규화가 필요한 태그를 일괄 갱신.

    save() 시점 정규화는 신규/갱신만 잡으므로, 옛날에 저장된 태그(V5, batch_api 등)는
    이 함수로 백필. PROTECTED 태그(v*_fixed)는 normalize_tag()가 보존.

    반환:
      {dry_run, team_id, scanned, changed, samples: [{id, before, after}, ...]}
    """
    mems = (
        session.query(Memory)
        .filter_by(team_id=team_id)
        .filter(Memory.archived_at.is_(None))
        .all()
    )
    changed = 0
    samples: list[dict] = []
    for m in mems:
        before = _parse_tags(m.tags)
        after = normalize_tags(before)
        if before == after:
            continue
        changed += 1
        if len(samples) < 20:
            samples.append({"id": m.id, "before": before, "after": after})
        if not dry_run:
            m.tags = json.dumps(after, ensure_ascii=False)
    if not dry_run:
        session.commit()
    return {
        "dry_run": dry_run,
        "team_id": team_id,
        "scanned": len(mems),
        "changed": changed,
        "samples": samples,
    }


def to_dict(mem: Memory) -> dict:
    return {
        "id": mem.id,
        "type": mem.mem_type,
        "description": mem.description,
        "content": mem.content,
        "confidence": mem.confidence,
        "tags": json.loads(mem.tags) if mem.tags else [],
        "platform": mem.source_platform,
        "captured_by": mem.captured_by or "",
        "session_id": mem.session_id or "",
        "created_at": _iso_utc(mem.created_at),
        "updated_at": _iso_utc(mem.updated_at),
        "last_verified_at": _iso_utc(mem.last_verified_at),
        "archived_at": _iso_utc(mem.archived_at),
    }
