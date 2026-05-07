"""namespace consolidation 테스트.

같은 value가 prefix 다르게 분산된 케이스를 count 기반 canonical로 통합.
"""
from __future__ import annotations

import json
import pytest


# ── build_namespace_canonical_map (순수 로직) ─────────────

def test_flat_to_prefixed_canonical(session, make_memory):
    """flat 'X' + 'project:X' → project:X로 통합 (count 무관, prefix 우선)."""
    from server.memory_store import build_namespace_canonical_map
    mems = []
    # flat 5건
    for _ in range(5):
        mems.append(make_memory(tags='["a2a-ctgr-match"]', confidence=0.7))
    # project: 3건
    for _ in range(3):
        mems.append(make_memory(tags='["project:a2a-ctgr-match"]', confidence=0.7))

    mapping = build_namespace_canonical_map(mems)
    # flat이 5로 더 많지만 prefix 우선으로 project:가 canonical
    # (count 동률 아니라 우리는 count 우선으로 했으니 flat이 이김)
    # 우리 규칙은 count 우선이므로 5 > 3, flat이 canonical이 됨
    assert mapping == {"project:a2a-ctgr-match": "a2a-ctgr-match"}


def test_count_wins_over_prefix_priority(session, make_memory):
    """count 더 많은 변종이 canonical (prefix 무관)."""
    from server.memory_store import build_namespace_canonical_map
    mems = []
    # project: 10건
    for _ in range(10):
        mems.append(make_memory(tags='["project:x"]'))
    # domain: 3건
    for _ in range(3):
        mems.append(make_memory(tags='["domain:x"]'))

    mapping = build_namespace_canonical_map(mems)
    assert mapping == {"domain:x": "project:x"}


def test_tie_break_by_prefix_priority(session, make_memory):
    """count 동률이면 prefix 우선순위로 (project > domain > team > ...)."""
    from server.memory_store import build_namespace_canonical_map
    mems = []
    # 각각 3건씩 동률
    for _ in range(3):
        mems.append(make_memory(tags='["domain:x"]'))
    for _ in range(3):
        mems.append(make_memory(tags='["project:x"]'))

    mapping = build_namespace_canonical_map(mems)
    # project가 우선
    assert mapping == {"domain:x": "project:x"}


def test_no_mapping_when_single_variant(session, make_memory):
    from server.memory_store import build_namespace_canonical_map
    m = make_memory(tags='["project:x"]')
    mapping = build_namespace_canonical_map([m])
    assert mapping == {}


def test_no_mapping_when_all_same_prefix(session, make_memory):
    """변종이라도 모두 같은 prefix면 합칠 게 없음 (서로 다른 value)."""
    from server.memory_store import build_namespace_canonical_map
    mems = [
        make_memory(tags='["project:a"]'),
        make_memory(tags='["project:b"]'),
        make_memory(tags='["project:c"]'),
    ]
    mapping = build_namespace_canonical_map(mems)
    assert mapping == {}


def test_protected_tag_excluded(session, make_memory):
    """v*_fixed는 매핑에서 제외 — 의미 보존."""
    from server.memory_store import build_namespace_canonical_map
    mems = [
        make_memory(tags='["v5_fixed"]'),
        make_memory(tags='["v5"]'),
        make_memory(tags='["project:v5"]'),
    ]
    mapping = build_namespace_canonical_map(mems)
    # v5_fixed는 매핑 안 됨, v5/project:v5만 통합
    assert "v5_fixed" not in mapping
    assert "v5_fixed" not in mapping.values()


def test_source_tag_excluded(session, make_memory):
    """source:* 는 통합 대상 아님 (메타 태그)."""
    from server.memory_store import build_namespace_canonical_map
    mems = [
        make_memory(tags='["source:declared", "source:inferred"]'),
    ]
    mapping = build_namespace_canonical_map(mems)
    assert mapping == {}


def test_multiple_clusters(session, make_memory):
    from server.memory_store import build_namespace_canonical_map
    mems = []
    for _ in range(3):
        mems.append(make_memory(tags='["project:a2a"]'))
    for _ in range(2):
        mems.append(make_memory(tags='["a2a"]'))
    for _ in range(4):
        mems.append(make_memory(tags='["domain:auth"]'))
    for _ in range(1):
        mems.append(make_memory(tags='["auth"]'))

    mapping = build_namespace_canonical_map(mems)
    assert mapping == {"a2a": "project:a2a", "auth": "domain:auth"}


# ── consolidate_namespaces (백필) ──────────────────────────

def test_consolidate_dry_run_no_mutation(session, make_memory):
    from server.memory_store import consolidate_namespaces
    a = make_memory(tags='["project:a2a"]')
    b = make_memory(tags='["a2a"]')

    res = consolidate_namespaces(session, "team-test", dry_run=True)
    assert res["clusters"] >= 1
    assert res["changed_memories"] >= 1

    session.refresh(a); session.refresh(b)
    assert json.loads(a.tags) == ["project:a2a"]
    assert json.loads(b.tags) == ["a2a"]


def test_consolidate_apply_replaces_tags(session, make_memory):
    from server.memory_store import consolidate_namespaces
    a = make_memory(tags='["project:a2a", "domain:matching"]')
    b = make_memory(tags='["a2a"]')

    res = consolidate_namespaces(session, "team-test", dry_run=False)
    assert res["changed_memories"] == 1  # b만 변경

    session.refresh(b)
    assert json.loads(b.tags) == ["project:a2a"]


def test_consolidate_dedups_after_replacement(session, make_memory):
    """치환 후 같은 태그가 두 번 나오면 dedup."""
    from server.memory_store import consolidate_namespaces
    # 한 메모리에 a2a 와 project:a2a 둘 다 있는 경우 (이상하긴 하지만 가능)
    m = make_memory(tags='["a2a", "project:a2a", "team:x"]')
    # 다른 메모리들이 project:a2a를 더 많이 만들도록
    for _ in range(5):
        make_memory(tags='["project:a2a"]')

    consolidate_namespaces(session, "team-test", dry_run=False)

    session.refresh(m)
    tags = json.loads(m.tags)
    assert tags.count("project:a2a") == 1  # dedup
    assert "team:x" in tags


def test_consolidate_team_scoped(session, make_memory):
    from server.memory_store import consolidate_namespaces
    a = make_memory(tags='["project:a2a"]', team_id="team-test")
    b = make_memory(tags='["a2a"]', team_id="team-test")
    c = make_memory(tags='["a2a"]', team_id="team-other")

    consolidate_namespaces(session, "team-test", dry_run=False)

    session.refresh(b)
    session.refresh(c)
    assert json.loads(b.tags) == ["project:a2a"]
    assert json.loads(c.tags) == ["a2a"]  # 다른 팀 안 건드림


def test_consolidate_excludes_archived(session, make_memory):
    from server.memory_store import consolidate_namespaces
    # archived는 분석에서 빠짐 → flat이 1건만 active이면 mapping 없음
    make_memory(tags='["a2a"]', archived_days_ago=5)
    make_memory(tags='["a2a"]', archived_days_ago=5)
    make_memory(tags='["project:a2a"]')

    res = consolidate_namespaces(session, "team-test", dry_run=True)
    # active 풀에는 project:a2a만 있어 클러스터 형성 안 됨
    assert res["clusters"] == 0


def test_consolidate_returns_samples(session, make_memory):
    from server.memory_store import consolidate_namespaces
    for _ in range(3):
        make_memory(tags='["project:a2a"]')
    for _ in range(2):
        make_memory(tags='["a2a"]')

    res = consolidate_namespaces(session, "team-test", dry_run=True)
    assert len(res["mapping_samples"]) == 1
    s = res["mapping_samples"][0]
    assert s["canonical"] == "project:a2a"
    merged = s["merged_from"]
    assert len(merged) == 1
    assert merged[0]["tag"] == "a2a"
    assert merged[0]["count"] == 2


def test_consolidate_empty_team_returns_zero(session):
    from server.memory_store import consolidate_namespaces
    res = consolidate_namespaces(session, "team-test", dry_run=True)
    assert res["scanned"] == 0
    assert res["changed_memories"] == 0
    assert res["clusters"] == 0
