"""ontology 카드 뷰 그룹화 로직 단위 테스트 — _build_ontology_cards.

server.main import이 모듈 로드 시점에 engine을 초기화하므로 conftest의
DATABASE_URL fixture가 먼저 적용되도록 함수 안에서 import한다.
"""
from __future__ import annotations

import pytest


def test_empty_returns_zero_total(session):
    from server.main import _build_ontology_cards
    out = _build_ontology_cards([])
    assert out == {"total": 0, "groups": []}


def test_groups_by_project_prefix_first(session, make_memory):
    """project: 가 domain: 보다 우선."""
    from server.main import _build_ontology_cards
    a = make_memory(
        mem_type="ontology", description="A 정의",
        tags='["project:a2a", "domain:matching", "source:declared"]',
    )
    b = make_memory(
        mem_type="ontology", description="B 정의",
        tags='["domain:matching", "source:declared"]',
    )
    out = _build_ontology_cards([a, b])

    assert out["total"] == 2
    tags = [g["tag"] for g in out["groups"]]
    # project: 그룹과 domain: 그룹이 분리됨
    assert "project:a2a" in tags
    assert "domain:matching" in tags


def test_uncategorized_when_no_namespace_tags(session, make_memory):
    from server.main import _build_ontology_cards
    a = make_memory(
        mem_type="ontology", description="태그 없음",
        tags='["source:declared"]',
    )
    out = _build_ontology_cards([a])
    assert out["groups"][0]["tag"] == "uncategorized"


def test_first_non_source_tag_used_when_no_prefix(session, make_memory):
    from server.main import _build_ontology_cards
    a = make_memory(
        mem_type="ontology", description="플랫 태그",
        tags='["source:declared", "billing"]',
    )
    out = _build_ontology_cards([a])
    assert out["groups"][0]["tag"] == "billing"


def test_groups_sorted_by_count_desc(session, make_memory):
    from server.main import _build_ontology_cards
    mems = []
    for i in range(3):
        mems.append(make_memory(
            mem_type="ontology", description=f"A항목 {i}",
            tags='["project:a2a"]',
        ))
    mems.append(make_memory(
        mem_type="ontology", description="B항목",
        tags='["project:b"]',
    ))
    out = _build_ontology_cards(mems)
    assert out["groups"][0]["tag"] == "project:a2a"
    assert out["groups"][0]["count"] == 3
    assert out["groups"][1]["tag"] == "project:b"


def test_items_sorted_by_confidence_desc(session, make_memory):
    from server.main import _build_ontology_cards
    a = make_memory(
        mem_type="ontology", description="저신뢰", confidence=0.5,
        tags='["project:x"]',
    )
    b = make_memory(
        mem_type="ontology", description="고신뢰", confidence=0.9,
        tags='["project:x"]',
    )
    out = _build_ontology_cards([a, b])
    items = out["groups"][0]["items"]
    assert items[0]["description"] == "고신뢰"
    assert items[1]["description"] == "저신뢰"


def test_item_includes_required_fields(session, make_memory):
    from server.main import _build_ontology_cards
    m = make_memory(
        mem_type="ontology", description="설명", confidence=0.8,
        tags='["project:x", "source:declared"]',
    )
    out = _build_ontology_cards([m])
    item = out["groups"][0]["items"][0]
    assert set(item.keys()) >= {
        "id", "description", "content", "tags", "confidence",
        "captured_by", "last_verified_at",
    }
    assert item["tags"] == ["project:x", "source:declared"]


def test_malformed_tags_treated_as_empty(session, make_memory):
    from server.main import _build_ontology_cards
    m = make_memory(mem_type="ontology", description="깨진 태그", tags="not-json")
    out = _build_ontology_cards([m])
    assert out["groups"][0]["tag"] == "uncategorized"
