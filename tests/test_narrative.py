"""Phase B: 도메인 narrative — DomainSummary.narrative 컬럼 동작 검증.

LLM 호출이 들어가는 rebuild_domain_narratives는 monkeypatch로 _llm_summarize를 가짜로 대체해서 동작 흐름만 검증.
"""
from __future__ import annotations

import pytest


def test_get_cached_includes_narrative(session, make_memory):
    """캐시 응답에 narrative 필드 포함."""
    from server.reporter import get_cached_domain_summaries
    from server.db import DomainSummary
    from datetime import datetime, timezone
    row = DomainSummary(
        team_id="team-test", tag="project:x",
        summary="한 줄 설명",
        narrative="## 배경\n시작\n## 결정의 흐름\n흐름\n## 현재\n상태\n## 미해결\n미상",
        memory_count=10,
        narrative_updated_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.commit()

    res = get_cached_domain_summaries(session, "team-test")
    assert "project:x" in res
    assert res["project:x"]["narrative"].startswith("## 배경")
    assert res["project:x"]["narrative_updated_at"] is not None


def test_get_cached_empty_narrative_normalized(session, make_memory):
    """narrative 미설정 row는 빈 문자열로 노출."""
    from server.reporter import get_cached_domain_summaries
    from server.db import DomainSummary
    row = DomainSummary(team_id="team-test", tag="project:y", summary="...", memory_count=1)
    session.add(row)
    session.commit()
    res = get_cached_domain_summaries(session, "team-test")
    assert res["project:y"]["narrative"] == ""


def test_rebuild_narratives_skips_below_min_count(session, make_memory, monkeypatch):
    """min_count 미만 도메인은 LLM 호출 안 함."""
    from server import reporter
    # 메모리 2개만 있는 도메인 — min_count=5에 못 미침
    for _ in range(2):
        make_memory(tags='["project:tiny"]')

    called = {"n": 0}
    def fake_llm(prompt, op, ctx):
        called["n"] += 1
        return "## 배경\n어쩌고\n## 결정의 흐름\n저쩌고\n## 현재\n현재상태\n## 미해결\n미상"
    monkeypatch.setattr(reporter, "_llm_summarize", fake_llm)

    res = reporter.rebuild_domain_narratives(session, "team-test", top_n=8, min_count=5)
    assert called["n"] == 0
    assert res["updated"] == 0
    assert res["total_targets"] == 0


def test_rebuild_narratives_writes_cache(session, make_memory, monkeypatch):
    """min_count 충족 도메인에 LLM 호출 → DomainSummary.narrative 저장."""
    from server import reporter
    from server.db import DomainSummary
    for i in range(6):
        make_memory(
            tags='["project:big"]',
            description=f"description 항목 번호 {i} 충분히 길게",
        )

    fake = "## 배경\n시작 배경\n## 결정의 흐름\n진화 흐름\n## 현재\n안정 상태\n## 미해결\n미상"
    monkeypatch.setattr(reporter, "_llm_summarize", lambda p, o, c: fake)

    res = reporter.rebuild_domain_narratives(session, "team-test", top_n=8, min_count=5)
    assert res["updated"] == 1

    row = session.query(DomainSummary).filter_by(team_id="team-test", tag="project:big").first()
    assert row is not None
    assert row.narrative == fake
    assert row.narrative_updated_at is not None


def test_rebuild_narratives_failure_keeps_existing(session, make_memory, monkeypatch):
    """LLM이 '(리포트 생성 실패' 반환하면 row 갱신 안 함."""
    from server import reporter
    from server.db import DomainSummary
    from datetime import datetime, timezone
    # 기존 narrative 있는 row (다른 테스트와 격리 위해 고유 태그)
    pre = DomainSummary(
        team_id="team-test", tag="project:fail-test", summary="x",
        narrative="기존 narrative", memory_count=10,
        narrative_updated_at=datetime.now(timezone.utc),
    )
    session.add(pre)
    for _ in range(6):
        make_memory(tags='["project:fail-test"]')
    session.commit()

    monkeypatch.setattr(
        reporter, "_llm_summarize",
        lambda p, o, c: "(리포트 생성 실패: timeout)",
    )

    res = reporter.rebuild_domain_narratives(session, "team-test", top_n=8, min_count=5)
    assert res["updated"] == 0
    session.refresh(pre)
    assert pre.narrative == "기존 narrative"  # 보존
