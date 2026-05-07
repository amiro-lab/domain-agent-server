"""retagger (untagged 메모리 백필) 단위 테스트.

LLM 호출은 monkeypatch로 stub.
"""
from __future__ import annotations

import json

import pytest


def _stub_llm(returns: dict[str, str]):
    """call 순서별 응답을 큐로 돌려주는 LLM stub."""
    queue = list(returns.values()) if isinstance(returns, dict) else list(returns)

    def _fake(prompt, operation="report", ctx=None):
        return queue.pop(0) if queue else ""
    return _fake


def test_parse_proposals_basic():
    from server.retagger import _parse_proposals
    raw = """
fact_test_0001: project:billing, domain:checkout
fact_test_0002: project:domain-agent
"""
    out = _parse_proposals(raw)
    assert out["fact_test_0001"] == ["project:billing", "domain:checkout"]
    assert out["fact_test_0002"] == ["project:domain-agent"]


def test_parse_proposals_skips_invalid_tags():
    """source: 또는 namespace 없는 태그는 버림."""
    from server.retagger import _parse_proposals
    raw = "fact_test_0001: source:declared, just-a-word, project:ok"
    out = _parse_proposals(raw)
    assert out["fact_test_0001"] == ["project:ok"]


def test_parse_proposals_caps_at_max():
    from server.retagger import _parse_proposals, MAX_TAGS_PER_ITEM
    tags = ", ".join(f"project:p{i}" for i in range(MAX_TAGS_PER_ITEM + 5))
    raw = f"fact_test_0001: {tags}"
    out = _parse_proposals(raw)
    assert len(out["fact_test_0001"]) == MAX_TAGS_PER_ITEM


def test_parse_proposals_handles_backticks_and_bullets():
    from server.retagger import _parse_proposals
    raw = """
- `fact_test_0001`: `project:x`, `domain:y`
* fact_test_0002 : project:z
"""
    out = _parse_proposals(raw)
    assert out["fact_test_0001"] == ["project:x", "domain:y"]
    assert out["fact_test_0002"] == ["project:z"]


def test_retag_untagged_dry_run_does_not_mutate(session, make_memory, monkeypatch):
    from server import retagger
    m = make_memory(tags="[]", description="test desc")
    fake = lambda *a, **kw: f"{m.id}: project:billing"
    monkeypatch.setattr(retagger, "_llm_summarize", fake)

    result = retagger.retag_untagged(session, "team-test", dry_run=True)
    session.refresh(m)
    assert result["dry_run"] is True
    assert result["applied"] == 0
    assert json.loads(m.tags) == []
    assert len(result["preview"]) == 1
    assert result["preview"][0]["new_tags"] == ["project:billing"]


def test_retag_untagged_apply_writes_merged_tags(session, make_memory, monkeypatch):
    from server import retagger
    from server.db import Memory
    m = make_memory(tags=json.dumps(["source:declared"]), description="test desc")
    fake = lambda *a, **kw: f"{m.id}: project:billing, domain:checkout"
    monkeypatch.setattr(retagger, "_llm_summarize", fake)

    retagger.retag_untagged(session, "team-test", dry_run=False)
    refreshed = session.get(Memory, m.id)
    new = json.loads(refreshed.tags)
    assert "source:declared" in new            # 기존 태그 보존
    assert "project:billing" in new            # 새 태그 부여
    assert "domain:checkout" in new


def test_retag_untagged_skips_already_tagged(session, make_memory, monkeypatch):
    """이미 namespace 태그 있는 메모리는 untagged 풀에서 제외."""
    from server import retagger
    tagged = make_memory(tags=json.dumps(["project:existing"]))
    monkeypatch.setattr(retagger, "_llm_summarize", lambda *a, **kw: "")

    result = retagger.retag_untagged(session, "team-test", dry_run=True)
    assert result["untagged_total"] == 0
    assert result["processed"] == 0


def test_retag_untagged_skips_archived(session, make_memory, monkeypatch):
    from server import retagger
    archived = make_memory(tags="[]", archived_days_ago=10)
    monkeypatch.setattr(retagger, "_llm_summarize", lambda *a, **kw: "")

    result = retagger.retag_untagged(session, "team-test", dry_run=True)
    assert result["untagged_total"] == 0


def test_retag_untagged_respects_limit(session, make_memory, monkeypatch):
    from server import retagger
    for _ in range(10):
        make_memory(tags="[]")
    monkeypatch.setattr(retagger, "_llm_summarize", lambda *a, **kw: "")

    result = retagger.retag_untagged(session, "team-test", dry_run=True, limit=3)
    assert result["untagged_total"] == 10   # 전체 카운트는 유지
    assert result["processed"] == 3          # 한정 처리


def test_retag_untagged_handles_llm_failure(session, make_memory, monkeypatch):
    """LLM 실패 시 해당 배치만 스킵, 나머지는 진행."""
    from server import retagger
    m1 = make_memory(tags="[]", description="m1")
    m2 = make_memory(tags="[]", description="m2")

    calls = {"n": 0}
    def fake(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("LLM down")
        return f"{m2.id}: project:x"
    monkeypatch.setattr(retagger, "_llm_summarize", fake)
    monkeypatch.setattr(retagger, "BATCH_SIZE", 1)

    result = retagger.retag_untagged(session, "team-test", dry_run=False)
    # 첫 배치 실패, 둘째 적용
    assert result["proposed"] == 1
    assert result["applied"] == 1


def test_retag_untagged_does_not_cross_team(session, make_memory, monkeypatch):
    from server import retagger
    own = make_memory(tags="[]", team_id="team-test")
    other = make_memory(tags="[]", team_id="team-other")
    monkeypatch.setattr(retagger, "_llm_summarize", lambda *a, **kw: "")

    result = retagger.retag_untagged(session, "team-test", dry_run=True)
    assert result["untagged_total"] == 1
    ids_in_preview = {p["id"] for p in result["preview"]}
    assert other.id not in ids_in_preview
