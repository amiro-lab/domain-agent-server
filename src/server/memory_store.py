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
         platform: str = "", captured_by: str = "") -> Memory:
    """upsert + fuzzy merge.

    1) description_hash 정확 일치 → 기존 항목 갱신 (콘텐츠/태그 교체, last_verified 갱신)
    2) (1) 없을 때: 들어온 항목에 protected tag 없으면 Jaccard로 유사 항목 탐색 → bump
       - confidence = min(0.95, max(기존, 신규))
       - tags = union
       - content/description은 기존 유지 (canonical)
       - last_verified_at = now, archived_at = None
    3) 둘 다 미스 → 신규 insert
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
        "created_at": _iso_utc(mem.created_at),
        "updated_at": _iso_utc(mem.updated_at),
        "last_verified_at": _iso_utc(mem.last_verified_at),
        "archived_at": _iso_utc(mem.archived_at),
    }
