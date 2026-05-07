"""cleanup pipeline (normalize + dup_scan) 일괄 실행 테스트."""
from __future__ import annotations

import json
import pytest


def _call(session, team_id, *, dry_run, dup_threshold=0.6):
    """엔드포인트 함수를 직접 호출 — TestClient 인프라 없이 로직 검증."""
    from server.memory_store import normalize_existing_tags
    from server.janitor import dup_scan_for_team

    norm = normalize_existing_tags(session, team_id, dry_run=dry_run)
    dup = dup_scan_for_team(session, team_id, dry_run=dry_run, threshold=dup_threshold)
    return {"normalize": norm, "dup_scan": dup}


def test_pipeline_dry_run_no_mutation(session, make_memory):
    a = make_memory(tags='["project:V5"]', description="alpha beta gamma delta")
    b = make_memory(tags='["project:v5"]', description="alpha beta gamma delta extra")

    res = _call(session, "team-test", dry_run=True)
    assert res["normalize"]["changed"] == 1  # V5→v5
    assert res["dup_scan"]["clusters"] >= 1

    # 미변경
    session.refresh(a)
    session.refresh(b)
    assert json.loads(a.tags) == ["project:V5"]
    assert a.archived_at is None
    assert b.archived_at is None


def test_pipeline_apply_normalizes_then_merges(session, make_memory):
    a = make_memory(
        tags='["project:V5"]',
        description="alpha beta gamma delta",
        confidence=0.7,
    )
    b = make_memory(
        tags='["project:v5"]',
        description="alpha beta gamma delta extra",
        confidence=0.85,
    )
    res = _call(session, "team-test", dry_run=False)

    # 1단계: V5 → v5
    assert res["normalize"]["changed"] == 1
    # 2단계: 두 항목이 같은 cluster로 묶여 머지
    assert res["dup_scan"]["merged"] == 1

    session.refresh(a)
    session.refresh(b)
    # b가 conf 더 높으므로 canonical
    assert b.archived_at is None
    assert a.archived_at is not None
    # canonical에 정규화된 태그 union으로 박혀있음
    canonical_tags = json.loads(b.tags)
    assert "project:v5" in canonical_tags


def test_pipeline_normalize_first_enables_dup_match(session, make_memory):
    """정규화 없이 dup-scan만 돌면 못 잡는 케이스가 정규화 후엔 잡혀야 함.

    실제 운영에서 V5 태그와 v5 태그는 다른 토큰이라 카운트도 분산됨.
    이 테스트는 그 시나리오의 회귀 방지.
    """
    # 동일한 description이라도 description_hash UNIQUE 제약 때문에
    # 토큰이 살짝 다른 형태로 만든다.
    a = make_memory(description="V5 운영 사양 정의 문서", tags='["project:V5"]')
    b = make_memory(description="V5 운영 사양 정의 안내", tags='["project:v5"]')

    res = _call(session, "team-test", dry_run=False)
    assert res["normalize"]["changed"] == 1  # V5 → v5

    # description Jaccard ≥ 0.6 + 같은 mem_type → 머지 (PROTECTED 아님)
    session.refresh(a)
    session.refresh(b)
    assert (a.archived_at is None) != (b.archived_at is None)  # 정확히 1개만 archive


def test_pipeline_team_scoped(session, make_memory):
    a = make_memory(tags='["project:V5"]', team_id="team-test")
    b = make_memory(tags='["project:V5"]', team_id="team-other")

    _call(session, "team-test", dry_run=False)

    session.refresh(a)
    session.refresh(b)
    # 정규화는 team-test에만 적용됨
    assert json.loads(a.tags) == ["project:v5"]
    assert json.loads(b.tags) == ["project:V5"]


def test_pipeline_returns_both_subresults(session, make_memory):
    make_memory(tags='["project:V5"]')
    res = _call(session, "team-test", dry_run=True)

    assert "normalize" in res and "dup_scan" in res
    assert "scanned" in res["normalize"]
    assert "changed" in res["normalize"]
    assert "active" in res["dup_scan"]
    assert "clusters" in res["dup_scan"]
    assert "merged" in res["dup_scan"]
