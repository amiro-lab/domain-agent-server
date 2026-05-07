"""Phase A: 세션 attribution — Memory.session_id, save() 보존, siblings 조회."""
from __future__ import annotations

import json
import pytest


# ── save()에서 session_id 처리 ────────────────────────────

def test_save_stores_session_id(session):
    from server.memory_store import save
    from server.db import Memory
    m = save(session, "team-test", "fact",
             "테스트 메모리 description입니다",
             "본문이고 충분히 길어요 detail",
             0.8, [], session_id="sess_abc123")
    session.refresh(m)
    assert m.session_id == "sess_abc123"


def test_save_default_session_id_empty(session):
    """session_id 미제공 시 빈 문자열."""
    from server.memory_store import save
    m = save(session, "team-test", "fact",
             "테스트 메모리 description입니다", "본문 충분히 길어요 detail body",
             0.8, [])
    session.refresh(m)
    assert m.session_id == ""


def test_save_upsert_preserves_existing_session_id(session):
    """description_hash 정확 일치 upsert 시 기존 session_id가 비어있으면 새 값 채움,
    있으면 보존."""
    from server.memory_store import save
    save(session, "team-test", "fact",
         "동일 description입니다 충분히", "v1 본문 충분히 길어요 here",
         0.7, [], session_id="sess_first")
    # 같은 description으로 다른 session_id로 다시 저장
    m = save(session, "team-test", "fact",
             "동일 description입니다 충분히", "v2 본문 길어요 더욱",
             0.8, [], session_id="sess_second")
    # 첫 session_id 보존 (attribution은 처음 capture된 세션)
    assert m.session_id == "sess_first"


def test_save_upsert_fills_empty_session_id(session, make_memory):
    """기존 메모리 session_id가 빈 경우(옛 데이터)는 새 값으로 채움."""
    from server.memory_store import save, _desc_hash
    from server.db import Memory
    # 기존 메모리에 session_id 비워둠
    old = make_memory(description="기존 description입니다 길게")
    old.session_id = ""
    session.commit()

    m = save(session, "team-test", "fact",
             old.description, "새 본문 충분히 길게 작성하고 있음",
             0.85, [], session_id="sess_new")
    assert m.id == old.id  # upsert
    assert m.session_id == "sess_new"


# ── to_dict 노출 ──────────────────────────────────────────

def test_to_dict_includes_session_id(session, make_memory):
    from server.memory_store import to_dict
    m = make_memory()
    m.session_id = "sess_xyz"
    session.commit()
    d = to_dict(m)
    assert d["session_id"] == "sess_xyz"


def test_to_dict_empty_session_id_returns_empty_string(session, make_memory):
    """session_id 미설정 메모리(옛 데이터)는 빈 문자열로 노출."""
    from server.memory_store import to_dict
    m = make_memory()  # session_id 명시 안 함 → default ""
    d = to_dict(m)
    assert d["session_id"] == ""