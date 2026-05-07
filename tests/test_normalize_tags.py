"""태그 정규화 + 백필 단위 테스트."""
from __future__ import annotations

import json
import pytest


# ── normalize_tag (단일) ──────────────────────────────────

def test_normalize_lowercase_value():
    from server.memory_store import normalize_tag
    assert normalize_tag("project:V5") == "project:v5"


def test_normalize_underscore_to_hyphen_in_value():
    from server.memory_store import normalize_tag
    assert normalize_tag("system:batch_api") == "system:batch-api"
    assert normalize_tag("domain:data_integrity") == "domain:data-integrity"


def test_normalize_lowercases_prefix():
    from server.memory_store import normalize_tag
    assert normalize_tag("Domain:Auth") == "domain:auth"


def test_normalize_protects_v_fixed():
    from server.memory_store import normalize_tag
    # v*_fixed는 의미 보존: 케이스/언더스코어 그대로
    assert normalize_tag("v3_fixed") == "v3_fixed"
    assert normalize_tag("v5_fixed") == "v5_fixed"
    assert normalize_tag("v10_fixed") == "v10_fixed"


def test_normalize_strips_whitespace():
    from server.memory_store import normalize_tag
    assert normalize_tag("  team:billing  ") == "team:billing"


def test_normalize_no_prefix_tag():
    from server.memory_store import normalize_tag
    assert normalize_tag("Billing_Backend") == "billing-backend"


def test_normalize_empty_returns_empty():
    from server.memory_store import normalize_tag
    assert normalize_tag("") == ""
    assert normalize_tag("   ") == ""


def test_normalize_handles_none():
    from server.memory_store import normalize_tag
    assert normalize_tag(None) == ""


# ── normalize_tags (리스트) ────────────────────────────────

def test_normalize_tags_dedups_after_normalize():
    from server.memory_store import normalize_tags
    out = normalize_tags(["project:V5", "project:v5", "project:v5"])
    assert out == ["project:v5"]


def test_normalize_tags_preserves_order():
    from server.memory_store import normalize_tags
    out = normalize_tags(["domain:auth", "team:billing", "tech:postgres"])
    assert out == ["domain:auth", "team:billing", "tech:postgres"]


def test_normalize_tags_empty_strings_dropped():
    from server.memory_store import normalize_tags
    out = normalize_tags(["project:v5", "", "  ", "domain:auth"])
    assert out == ["project:v5", "domain:auth"]


# ── save() 자동 정규화 ─────────────────────────────────────

def test_save_normalizes_tags_at_insert(session):
    from server.memory_store import save
    from server.db import Memory
    save(session, "team-test", "fact", "테스트", "본문", 0.8,
         ["project:V5", "Domain:Auth", "batch_api"])
    m = session.query(Memory).first()
    tags = json.loads(m.tags)
    assert tags == ["project:v5", "domain:auth", "batch-api"]


def test_save_preserves_protected_tag(session):
    from server.memory_store import save
    from server.db import Memory
    save(session, "team-test", "fact", "v5 운영", "본문", 0.8,
         ["v5_fixed", "project:A2A"])
    m = session.query(Memory).first()
    tags = json.loads(m.tags)
    assert "v5_fixed" in tags  # 케이스 보존
    assert "project:a2a" in tags  # 다른 태그는 정규화


# ── normalize_existing_tags 백필 ───────────────────────────

def test_backfill_dry_run_reports_changes_no_mutation(session, make_memory):
    from server.memory_store import normalize_existing_tags
    m = make_memory(tags='["project:V5", "batch_api"]')
    res = normalize_existing_tags(session, "team-test", dry_run=True)

    assert res["dry_run"] is True
    assert res["scanned"] == 1
    assert res["changed"] == 1
    assert res["samples"][0]["before"] == ["project:V5", "batch_api"]
    assert res["samples"][0]["after"] == ["project:v5", "batch-api"]

    # 미변경 검증
    session.refresh(m)
    assert json.loads(m.tags) == ["project:V5", "batch_api"]


def test_backfill_apply_updates_tags(session, make_memory):
    from server.memory_store import normalize_existing_tags
    m = make_memory(tags='["project:V5", "batch_api"]')
    res = normalize_existing_tags(session, "team-test", dry_run=False)

    assert res["changed"] == 1
    session.refresh(m)
    assert json.loads(m.tags) == ["project:v5", "batch-api"]


def test_backfill_skips_already_canonical(session, make_memory):
    from server.memory_store import normalize_existing_tags
    make_memory(tags='["project:v5", "domain:auth"]')
    res = normalize_existing_tags(session, "team-test", dry_run=False)

    assert res["scanned"] == 1
    assert res["changed"] == 0
    assert res["samples"] == []


def test_backfill_excludes_archived(session, make_memory):
    from server.memory_store import normalize_existing_tags
    make_memory(tags='["project:V5"]', archived_days_ago=5)
    make_memory(tags='["project:V5"]')
    res = normalize_existing_tags(session, "team-test", dry_run=True)

    # archived 1건 제외, active 1건만 스캔
    assert res["scanned"] == 1
    assert res["changed"] == 1


def test_backfill_team_scoped(session, make_memory):
    from server.memory_store import normalize_existing_tags
    a = make_memory(tags='["project:V5"]', team_id="team-test")
    b = make_memory(tags='["project:V5"]', team_id="team-other")

    res = normalize_existing_tags(session, "team-test", dry_run=False)
    assert res["changed"] == 1

    # 다른 팀 미변경
    session.refresh(b)
    assert json.loads(b.tags) == ["project:V5"]


def test_backfill_preserves_protected_tags(session, make_memory):
    from server.memory_store import normalize_existing_tags
    m = make_memory(tags='["v5_fixed", "project:V5"]')
    res = normalize_existing_tags(session, "team-test", dry_run=False)

    session.refresh(m)
    tags = json.loads(m.tags)
    assert "v5_fixed" in tags        # protected 그대로
    assert "project:v5" in tags      # 일반 태그는 정규화
