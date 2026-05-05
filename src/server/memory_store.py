"""팀 메모리 CRUD."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from server.db import Memory


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


def save(session: Session, team_id: str, mem_type: str, description: str,
         content: str, confidence: float, tags: list[str],
         platform: str = "", captured_by: str = "") -> Memory:
    d_hash = _desc_hash(description)
    existing = session.query(Memory).filter_by(team_id=team_id, description_hash=d_hash).first()
    if existing:
        existing.content = content
        existing.confidence = confidence
        existing.tags = json.dumps(tags, ensure_ascii=False)
        existing.source_platform = platform
        if captured_by:
            existing.captured_by = captured_by
        session.commit()
        return existing

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
    )
    session.add(mem)
    session.commit()
    return mem


def query(session: Session, team_id: str, q: str, limit: int = 10) -> list[Memory]:
    all_mems = session.query(Memory).filter_by(team_id=team_id).all()
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
    q = session.query(Memory).filter_by(team_id=team_id)
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
    }
