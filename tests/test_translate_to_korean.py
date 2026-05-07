"""영어 메모리 → 한국어 번역 백필 테스트.

LLM 호출은 dry_run=True 경로로만 검증 (apply 경로는 통합 테스트 영역).
heuristic _looks_english와 후보 선정·dry-run 결과 구조 검증.
"""
from __future__ import annotations

import json
import pytest


# ── _looks_english heuristic ──────────────────────────────

def test_pure_korean_not_english():
    from server.memory_store import _looks_english
    assert _looks_english("결제팀 PM은 김OO이며 정산 담당", "본문 한국어") is False


def test_korean_with_english_terms_not_english():
    """일반적인 한국어+영어 코드 메모리는 영어 아님."""
    from server.memory_store import _looks_english
    assert _looks_english(
        "domain-agent MCP 서버 구현",
        "claude-agent-sdk를 사용해 Pro/Max 구독 인증을 자동 재사용",
    ) is False


def test_pure_english_is_english():
    from server.memory_store import _looks_english
    assert _looks_english(
        "Event formatting with speaker attribution is critical",
        "Multi-agent conversation requires speaker attribution to identify who is talking.",
    ) is True


def test_short_english_below_threshold_not_english():
    """라틴 글자 30 미만이면 영어로 판정 안 함."""
    from server.memory_store import _looks_english
    assert _looks_english("hi", "ok") is False


def test_empty_strings_not_english():
    from server.memory_store import _looks_english
    assert _looks_english("", "") is False
    assert _looks_english(None, None) is False


def test_single_korean_char_disqualifies():
    """한글 단 한 글자만 있어도 영어 아님 (보수적 판정)."""
    from server.memory_store import _looks_english
    long_eng = "this is a long english description with many tokens"
    assert _looks_english(f"{long_eng} 이", "more english content here") is False


# ── find_english_memories ─────────────────────────────────

def test_find_returns_only_english(session, make_memory):
    from server.memory_store import find_english_memories
    ko = make_memory(description="한국어 설명입니다", tags='[]')
    en = make_memory(
        description="Pure English description with enough latin characters here",
        tags='[]',
    )
    en.content = "Multi-agent simulation requires careful event formatting and speaker attribution"
    session.commit()

    res = find_english_memories(session, "team-test")
    ids = [m.id for m in res]
    assert en.id in ids
    assert ko.id not in ids


def test_find_excludes_archived(session, make_memory):
    from server.memory_store import find_english_memories
    archived = make_memory(
        description="English desc enough characters here ya know",
        archived_days_ago=5,
    )
    archived.content = "More english content with plenty of latin alphabet text"
    session.commit()

    res = find_english_memories(session, "team-test")
    assert archived.id not in [m.id for m in res]


def test_find_team_scoped(session, make_memory):
    from server.memory_store import find_english_memories
    a = make_memory(
        description="English desc for team test plenty of latin",
        team_id="team-test",
    )
    a.content = "lots of english text here in this content body"
    b = make_memory(
        description="English desc for team other plenty of latin",
        team_id="team-other",
    )
    b.content = "lots of english text here in this other content"
    session.commit()

    res = find_english_memories(session, "team-test")
    assert a.id in [m.id for m in res]
    assert b.id not in [m.id for m in res]


def test_find_respects_limit(session, make_memory):
    from server.memory_store import find_english_memories
    for i in range(5):
        m = make_memory(
            description=f"English desc number {i} with plenty of latin chars",
        )
        m.content = f"more english content body number {i} here lots"
    session.commit()

    res = find_english_memories(session, "team-test", limit=3)
    assert len(res) == 3


# ── translate_english_memories dry-run ────────────────────

def test_translate_dry_run_returns_candidates_no_llm(session, make_memory):
    """dry_run=True 면 LLM 호출 없이 후보 카운트와 샘플만."""
    from server.memory_store import translate_english_memories
    m = make_memory(
        description="English description plenty of latin alpha characters",
    )
    m.content = "More english content body with sufficient text for detection"
    session.commit()

    res = translate_english_memories(session, "team-test", dry_run=True)
    assert res["dry_run"] is True
    assert res["candidates"] >= 1
    assert res["translated"] == 0
    assert any("description" in s and "content_preview" in s for s in res["samples"])


def test_translate_dry_run_excludes_protected(session, make_memory):
    """PROTECTED 태그(v*_fixed)는 후보에서 제외."""
    from server.memory_store import translate_english_memories
    m = make_memory(
        description="English description plenty of latin alpha characters",
        tags='["v5_fixed"]',
    )
    m.content = "English content body with sufficient text for detection here"
    session.commit()

    res = translate_english_memories(session, "team-test", dry_run=True)
    assert res["candidates"] == 0
    assert res["samples"] == []


def test_translate_no_candidates_returns_zero(session, make_memory):
    """모든 메모리가 한국어면 후보 0."""
    from server.memory_store import translate_english_memories
    make_memory(description="순수 한국어 설명입니다 충분히 깁니다")
    res = translate_english_memories(session, "team-test", dry_run=True)
    assert res["candidates"] == 0
    assert res["translated"] == 0
