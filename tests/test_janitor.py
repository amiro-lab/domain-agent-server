"""janitor 단위 테스트.

커버 영역:
  - confidence_decay: grace window, 감쇠 공식, dry_run, archived 제외
  - soft_archive: 임계값, cap 가드, 이미 archived는 재처리 X
  - dup_scan: description 중복 탐지
  - tag_skew_alert: 단일 태그 비중 임계
  - hard_delete: HARD_DELETE_DAYS 게이트, dry_run
  - memory_store.save: last_verified_at 갱신, archived 재활성
  - memory_store.query/list_all: archived 제외 필터
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone

import pytest


def _to_aware(dt):
    """SQLite 라운드트립 후 dt는 naive UTC로 들어온다 — 비교용으로 tzinfo 부여."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ── confidence_decay ──────────────────────────────────────

def test_decay_skips_within_grace_window(session, make_memory):
    from server import janitor
    make_memory(confidence=0.7, age_days=10)   # grace 안
    make_memory(confidence=0.7, age_days=29)   # grace 경계 안
    janitor.confidence_decay()
    from server.db import Memory
    for m in session.query(Memory).all():
        assert m.confidence == 0.7


def test_decay_after_grace_applies_per_day_rate(session, make_memory):
    from server import janitor
    m = make_memory(confidence=0.9, age_days=60)
    janitor.confidence_decay()
    session.refresh(m)
    expected = 0.9 - (60 - janitor.DECAY_GRACE_DAYS) * janitor.DECAY_PER_DAY  # -0.15
    assert m.confidence == pytest.approx(expected, abs=1e-6)


def test_decay_floors_at_zero(session, make_memory):
    from server import janitor
    m = make_memory(confidence=0.05, age_days=365)
    janitor.confidence_decay()
    session.refresh(m)
    assert m.confidence == 0.0


def test_decay_dry_run_does_not_mutate(session, make_memory, monkeypatch):
    from server import janitor
    monkeypatch.setattr(janitor, "DRY_RUN", True)
    m = make_memory(confidence=0.9, age_days=60)
    janitor.confidence_decay()
    session.refresh(m)
    assert m.confidence == 0.9


def test_decay_skips_archived(session, make_memory):
    from server import janitor
    m = make_memory(confidence=0.9, age_days=60, archived_days_ago=10)
    janitor.confidence_decay()
    session.refresh(m)
    assert m.confidence == 0.9   # archived는 감쇠 대상 아님


# ── soft_archive ──────────────────────────────────────────

def test_soft_archive_marks_below_threshold(session, make_memory, relaxed_cap):
    from server import janitor
    low = make_memory(confidence=0.20)
    high = make_memory(confidence=0.50)
    janitor.soft_archive()
    session.refresh(low)
    session.refresh(high)
    assert low.archived_at is not None
    assert high.archived_at is None


def test_soft_archive_threshold_is_strict(session, make_memory, relaxed_cap):
    """confidence == ARCHIVE_THRESHOLD 이면 보존 (< 임계만 archive)."""
    from server import janitor
    edge = make_memory(confidence=janitor.ARCHIVE_THRESHOLD)
    janitor.soft_archive()
    session.refresh(edge)
    assert edge.archived_at is None


def test_soft_archive_cap_guard_aborts(session, make_memory, monkeypatch):
    """cap 초과 시 아무것도 변경하지 않아야 한다."""
    from server import janitor
    monkeypatch.setattr(janitor, "DAILY_CAP_RATIO", 0.05)
    # 10개 중 5개가 low → 50% > 5% 초과 → 중단
    for _ in range(5):
        make_memory(confidence=0.1)
    for _ in range(5):
        make_memory(confidence=0.9)
    janitor.soft_archive()
    from server.db import Memory
    archived = session.query(Memory).filter(Memory.archived_at.isnot(None)).count()
    assert archived == 0


def test_soft_archive_skips_already_archived(session, make_memory, relaxed_cap):
    from server import janitor
    already = make_memory(confidence=0.1, archived_days_ago=5)
    original_archived_at = already.archived_at
    janitor.soft_archive()
    session.refresh(already)
    assert already.archived_at == original_archived_at


def test_soft_archive_dry_run_does_not_mutate(session, make_memory, monkeypatch, relaxed_cap):
    from server import janitor
    monkeypatch.setattr(janitor, "DRY_RUN", True)
    m = make_memory(confidence=0.1)
    janitor.soft_archive()
    session.refresh(m)
    assert m.archived_at is None


# ── dup_scan: 자동 머지 ──────────────────────────────────────

def test_dup_scan_merges_jaccard_cluster_within_team(session, make_memory, relaxed_cap):
    """같은 team+mem_type에서 description Jaccard ≥ 0.6 → canonical 1개로 머지."""
    from server import janitor
    from server.db import Memory

    a = make_memory(
        description="domain-agent MCP 서버 구현 Claude Code 통합",
        confidence=0.7,
        tags='["domain:mcp"]',
    )
    b = make_memory(
        description="domain-agent MCP 서버 구현 Claude Code 네이티브 통합",
        confidence=0.85,
        tags='["domain:integration"]',
    )
    janitor.dup_scan()

    # 둘 중 conf 높은 b가 canonical, a는 archive
    session.refresh(a)
    session.refresh(b)
    assert b.archived_at is None
    assert a.archived_at is not None
    # canonical에 양쪽 태그 union
    canonical_tags = json.loads(b.tags)
    assert "domain:mcp" in canonical_tags
    assert "domain:integration" in canonical_tags
    assert b.confidence == pytest.approx(0.85)


def test_dup_scan_does_not_cross_team(session, make_memory, relaxed_cap):
    """팀이 다르면 같은 description이라도 머지 안 함."""
    from server import janitor
    from server.db import Memory

    a = make_memory(description="공유 설명", team_id="team-test")
    b = make_memory(description="공유 설명", team_id="team-other")
    janitor.dup_scan()

    session.refresh(a)
    session.refresh(b)
    assert a.archived_at is None
    assert b.archived_at is None


def test_dup_scan_does_not_cross_mem_type(session, make_memory, relaxed_cap):
    """mem_type이 다르면 토큰 거의 같아도 머지 안 함.

    UNIQUE 제약이 (team_id, description_hash)라 완전히 같은 description은 못 만들지만
    Jaccard ≥ 0.6 수준의 유사 description으로 mem_type 분리 검증.
    """
    from server import janitor

    a = make_memory(description="작은 PR 선호", mem_type="fact")
    b = make_memory(description="작은 PR 선호 항상", mem_type="preference")
    janitor.dup_scan()

    session.refresh(a)
    session.refresh(b)
    assert a.archived_at is None
    assert b.archived_at is None


def test_dup_scan_skips_protected_tag(session, make_memory, relaxed_cap):
    """v*_fixed 태그가 있는 항목은 의미 보존 — 머지 대상에서 제외."""
    from server import janitor

    a = make_memory(
        description="V5 파이프라인 운영 사양",
        confidence=0.8,
        tags='["v5_fixed", "project:a2a-ctgr-match"]',
    )
    b = make_memory(
        description="V5 파이프라인 운영 사양 동일",
        confidence=0.7,
        tags='["project:a2a-ctgr-match"]',
    )
    janitor.dup_scan()

    session.refresh(a)
    session.refresh(b)
    assert a.archived_at is None  # protected
    assert b.archived_at is None  # 짝꿍이 protected라 단독, 클러스터 형성 안 됨


def test_dup_scan_excludes_already_archived(session, make_memory, relaxed_cap):
    """이미 archive된 항목은 active 풀에서 제외."""
    from server import janitor

    archived = make_memory(description="A 비슷한 설명", archived_days_ago=5)
    active = make_memory(description="A 비슷한 설명 변형")
    janitor.dup_scan()

    session.refresh(active)
    assert active.archived_at is None  # 짝이 archived라 클러스터 미형성


def test_dup_scan_dry_run_does_not_mutate(session, make_memory, monkeypatch, relaxed_cap):
    from server import janitor
    monkeypatch.setattr(janitor, "DRY_RUN", True)

    a = make_memory(description="alpha beta gamma delta", confidence=0.7)
    b = make_memory(description="alpha beta gamma delta extra", confidence=0.85)
    janitor.dup_scan()

    session.refresh(a)
    session.refresh(b)
    assert a.archived_at is None
    assert b.archived_at is None
    assert a.confidence == pytest.approx(0.7)


def test_dup_scan_cap_guard_aborts(session, make_memory, monkeypatch):
    """전체의 5% 초과 변동이면 거부 (rollback)."""
    from server import janitor
    # cap 매우 작게 — 즉 어떤 변동이든 초과
    monkeypatch.setattr(janitor, "DAILY_CAP_RATIO", 0.01)

    a = make_memory(description="공유 토큰 alpha beta gamma", confidence=0.7)
    b = make_memory(description="공유 토큰 alpha beta gamma 변형", confidence=0.8)
    janitor.dup_scan()

    session.refresh(a)
    session.refresh(b)
    assert a.archived_at is None
    assert b.archived_at is None  # cap 초과로 rollback


def test_dup_scan_no_clusters_is_noop(session, make_memory, relaxed_cap):
    from server import janitor

    a = make_memory(description="apple banana")
    b = make_memory(description="zebra ostrich")  # 토큰 겹침 0
    janitor.dup_scan()

    session.refresh(a)
    session.refresh(b)
    assert a.archived_at is None
    assert b.archived_at is None


# ── memory_store.save: fuzzy merge ────────────────────────

def test_save_fuzzy_merges_similar_description(session):
    from server.memory_store import save

    save(session, "team-test", "fact",
         "domain-agent MCP 서버 구현 Claude Code 통합", "본문1", 0.7, ["domain:mcp"])
    save(session, "team-test", "fact",
         "domain-agent MCP 서버 구현 Claude Code 네이티브 통합", "본문2", 0.85, ["domain:integration"])

    from server.db import Memory
    active = session.query(Memory).filter(Memory.archived_at.is_(None)).all()
    assert len(active) == 1
    m = active[0]
    assert m.confidence == pytest.approx(0.85)  # max
    tags = json.loads(m.tags)
    assert "domain:mcp" in tags and "domain:integration" in tags


def test_save_fuzzy_skips_when_protected_tag_in_new_item(session):
    """들어오는 항목에 protected 태그가 있으면 fuzzy merge 안 함 — 신규 insert."""
    from server.memory_store import save
    from server.db import Memory

    save(session, "team-test", "fact",
         "V5 파이프라인 운영 사양", "본문 일반", 0.7, ["project:x"])
    save(session, "team-test", "fact",
         "V5 파이프라인 운영 사양 변형", "본문 v5", 0.8, ["v5_fixed"])

    active = session.query(Memory).filter(Memory.archived_at.is_(None)).all()
    assert len(active) == 2


def test_save_fuzzy_skips_when_protected_tag_in_existing(session):
    """기존 후보가 protected 태그면 fuzzy merge 대상에서 제외."""
    from server.memory_store import save
    from server.db import Memory

    save(session, "team-test", "fact",
         "V5 파이프라인 운영 사양", "본문 v5", 0.8, ["v5_fixed"])
    save(session, "team-test", "fact",
         "V5 파이프라인 운영 사양 변형", "본문 일반", 0.7, [])

    active = session.query(Memory).filter(Memory.archived_at.is_(None)).all()
    assert len(active) == 2


def test_save_exact_hash_path_still_overwrites(session):
    """description_hash 정확 일치 경로는 기존대로 콘텐츠 교체 (fuzzy 영향 없음)."""
    from server.memory_store import save
    from server.db import Memory

    save(session, "team-test", "fact", "동일한 설명", "v1", 0.7, ["t1"])
    save(session, "team-test", "fact", "동일한 설명", "v2", 0.9, ["t2"])

    active = session.query(Memory).filter(Memory.archived_at.is_(None)).all()
    assert len(active) == 1
    m = active[0]
    assert m.content == "v2"
    assert json.loads(m.tags) == ["t2"]  # 정확 일치는 교체


def test_save_fuzzy_does_not_cross_team(session):
    from server.memory_store import save
    from server.db import Memory

    save(session, "team-test", "fact", "alpha beta gamma delta", "x", 0.7, [])
    save(session, "team-other", "fact", "alpha beta gamma delta extra", "y", 0.7, [])

    active = session.query(Memory).filter(Memory.archived_at.is_(None)).all()
    assert len(active) == 2


def test_save_fuzzy_does_not_cross_mem_type(session):
    from server.memory_store import save
    from server.db import Memory

    save(session, "team-test", "fact", "alpha beta gamma delta", "x", 0.7, [])
    save(session, "team-test", "preference", "alpha beta gamma delta extra", "y", 0.7, [])

    active = session.query(Memory).filter(Memory.archived_at.is_(None)).all()
    assert len(active) == 2


def test_save_fuzzy_caps_confidence(session):
    from server.memory_store import save
    from server.db import Memory

    save(session, "team-test", "fact", "alpha beta gamma delta", "x", 0.9, [])
    save(session, "team-test", "fact", "alpha beta gamma delta extra", "y", 0.99, [])

    active = session.query(Memory).filter(Memory.archived_at.is_(None)).all()
    assert len(active) == 1
    assert active[0].confidence <= 0.95


# ── dup_scan_for_team: 수동 트리거 ──────────────────────────

def test_dup_scan_for_team_dry_run_returns_clusters_no_mutation(session, make_memory):
    from server import janitor

    a = make_memory(description="alpha beta gamma delta", confidence=0.7)
    b = make_memory(description="alpha beta gamma delta extra", confidence=0.85)
    result = janitor.dup_scan_for_team(session, "team-test", dry_run=True)

    assert result["dry_run"] is True
    assert result["clusters"] == 1
    assert result["merged"] == 1
    assert len(result["details"]) == 1
    # 미변경 검증
    session.refresh(a)
    session.refresh(b)
    assert a.archived_at is None
    assert b.archived_at is None


def test_dup_scan_for_team_apply_archives_non_canonical(session, make_memory):
    from server import janitor

    a = make_memory(
        description="alpha beta gamma delta", confidence=0.7, tags='["t1"]',
    )
    b = make_memory(
        description="alpha beta gamma delta extra", confidence=0.85, tags='["t2"]',
    )
    result = janitor.dup_scan_for_team(session, "team-test", dry_run=False)

    assert result["merged"] == 1
    session.refresh(a)
    session.refresh(b)
    # b가 conf 더 높아 canonical, a는 archive
    assert b.archived_at is None
    assert a.archived_at is not None
    canonical_tags = json.loads(b.tags)
    assert "t1" in canonical_tags and "t2" in canonical_tags


def test_dup_scan_for_team_scoped_to_team(session, make_memory):
    """다른 팀 메모리는 안 건드림."""
    from server import janitor

    a = make_memory(description="alpha beta gamma delta", team_id="team-test")
    b = make_memory(description="alpha beta gamma delta extra", team_id="team-test")
    other = make_memory(description="alpha beta gamma delta", team_id="team-other")
    janitor.dup_scan_for_team(session, "team-test", dry_run=False)

    session.refresh(other)
    assert other.archived_at is None  # team-other은 무관


def test_dup_scan_for_team_skips_protected_tag(session, make_memory):
    from server import janitor

    a = make_memory(description="V5 운영 사양", confidence=0.8, tags='["v5_fixed"]')
    b = make_memory(description="V5 운영 사양 동일", confidence=0.7, tags='["other"]')
    result = janitor.dup_scan_for_team(session, "team-test", dry_run=False)

    assert result["merged"] == 0
    session.refresh(a)
    session.refresh(b)
    assert a.archived_at is None
    assert b.archived_at is None


def test_dup_scan_for_team_threshold_param(session, make_memory):
    """threshold 0.95로 올리면 거의 동일한 것만 잡혀 머지 0건."""
    from server import janitor

    make_memory(description="alpha beta gamma delta", confidence=0.7)
    make_memory(description="alpha beta gamma delta extra", confidence=0.85)
    result = janitor.dup_scan_for_team(session, "team-test", dry_run=True, threshold=0.95)

    assert result["clusters"] == 0
    assert result["merged"] == 0


def test_dup_scan_for_team_no_cap_guard(session, make_memory):
    """수동 트리거는 cap 제한 없음 — backlog 청소 가능."""
    from server import janitor

    # 8개 중 6개가 같은 클러스터(75% 머지) — cron이면 cap으로 차단됐을 비율
    for i in range(6):
        make_memory(description=f"공유 토큰 alpha beta gamma 변형 {i}", confidence=0.7 + i * 0.01)
    make_memory(description="완전히 별개 zebra ostrich")
    make_memory(description="또 다른 별개 yak")

    result = janitor.dup_scan_for_team(session, "team-test", dry_run=False)
    assert result["merged"] >= 5  # 6개 중 5개는 archive

    from server.db import Memory
    archived = session.query(Memory).filter(Memory.archived_at.isnot(None)).count()
    assert archived >= 5


# ── tag_skew_alert ────────────────────────────────────────

def test_tag_skew_alert_no_data_does_not_fail(session):
    from server import janitor
    janitor.tag_skew_alert()  # 빈 DB에서도 죽지 않아야 함


def test_tag_skew_alert_triggers_above_20pct(session, make_memory, caplog):
    """단일 태그가 25% 차지 → WARNING 발생."""
    from server import janitor
    import logging
    janitor.log.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger="janitor")
    for _ in range(3):
        make_memory(tags=json.dumps(["domain:hot"]))
    for _ in range(9):
        make_memory(tags=json.dumps(["domain:misc"]))
    janitor.tag_skew_alert()
    assert any("tag_skew" in r.message and "domain:" in r.message for r in caplog.records)


# ── hard_delete ───────────────────────────────────────────

def test_hard_delete_removes_old_archived(session, make_memory, relaxed_cap):
    from server import janitor
    old = make_memory(archived_days_ago=janitor.HARD_DELETE_DAYS + 5)
    old_id = old.id
    janitor.hard_delete()
    from server.db import Memory
    assert session.query(Memory).filter_by(id=old_id).first() is None


def test_hard_delete_keeps_recent_archive(session, make_memory, relaxed_cap):
    from server import janitor
    recent = make_memory(archived_days_ago=janitor.HARD_DELETE_DAYS - 5)
    janitor.hard_delete()
    session.refresh(recent)  # 살아있어야 refresh 성공
    assert recent.archived_at is not None


def test_hard_delete_skips_active(session, make_memory, relaxed_cap):
    from server import janitor
    active = make_memory(archived_days_ago=None)  # archived_at IS NULL
    janitor.hard_delete()
    session.refresh(active)
    assert active.archived_at is None


def test_hard_delete_dry_run_does_not_delete(session, make_memory, monkeypatch, relaxed_cap):
    from server import janitor
    monkeypatch.setattr(janitor, "DRY_RUN", True)
    m = make_memory(archived_days_ago=janitor.HARD_DELETE_DAYS + 5)
    mid = m.id
    janitor.hard_delete()
    from server.db import Memory
    assert session.query(Memory).filter_by(id=mid).first() is not None


# ── snapshot ──────────────────────────────────────────────

def test_snapshot_copies_db_and_rotates_old(session, tmp_path, monkeypatch):
    from server import janitor
    fake_db = tmp_path / "db.sqlite"
    fake_db.write_bytes(b"sqlite3-mock")
    backup_dir = tmp_path / "backups"

    monkeypatch.setattr(janitor, "DB_PATH", fake_db)
    monkeypatch.setattr(janitor, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(janitor, "BACKUP_RETENTION", 14)

    janitor.snapshot()
    backups = list(backup_dir.glob("domain_agent_*.db"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"sqlite3-mock"


def test_snapshot_dry_run_no_copy(session, tmp_path, monkeypatch):
    from server import janitor
    fake_db = tmp_path / "db.sqlite"
    fake_db.write_bytes(b"x")
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(janitor, "DB_PATH", fake_db)
    monkeypatch.setattr(janitor, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(janitor, "DRY_RUN", True)

    janitor.snapshot()
    assert not list(backup_dir.glob("domain_agent_*.db"))


def test_snapshot_skips_when_db_missing(session, tmp_path, monkeypatch):
    from server import janitor
    monkeypatch.setattr(janitor, "DB_PATH", tmp_path / "does_not_exist.db")
    monkeypatch.setattr(janitor, "BACKUP_DIR", tmp_path / "backups")
    janitor.snapshot()  # 죽지 않아야 함


# ── memory_store: last_verified_at / archived_at 동작 ───────

def test_save_sets_last_verified_at_on_create(session):
    from server import memory_store
    before = datetime.now(timezone.utc) - timedelta(seconds=2)
    m = memory_store.save(session, "team-test", "fact", "신규 메모리", "내용", 0.8, [])
    assert m.last_verified_at is not None
    assert _to_aware(m.last_verified_at) >= before


def test_save_refreshes_last_verified_at_on_update(session, make_memory):
    from server import memory_store
    from server.db import Memory
    old = make_memory(age_days=60, description="기존")
    old_verified = _to_aware(old.last_verified_at)
    memory_store.save(session, "team-test", "fact", "기존", "갱신된 내용", 0.7, [])
    rows = session.query(Memory).filter_by(description="기존").all()
    assert len(rows) == 1, "save는 description_hash로 upsert되어야 한다"
    assert _to_aware(rows[0].last_verified_at) > old_verified


def test_save_clears_archived_at_on_resave(session, make_memory):
    """archived 메모리를 동일 description으로 재저장하면 reactivate."""
    from server import memory_store
    from server.db import Memory
    archived = make_memory(description="복귀할 메모리", archived_days_ago=10)
    assert archived.archived_at is not None
    memory_store.save(session, "team-test", "fact", "복귀할 메모리", "재내용", 0.7, [])
    refreshed = session.query(Memory).filter_by(description="복귀할 메모리").first()
    assert refreshed.archived_at is None


def test_query_excludes_archived(session, make_memory):
    from server import memory_store
    make_memory(description="활성")
    make_memory(description="죽은", archived_days_ago=5)
    results = memory_store.query(session, "team-test", "")
    descs = [m.description for m in results]
    assert "활성" in descs
    assert "죽은" not in descs


def test_list_all_excludes_archived(session, make_memory):
    from server import memory_store
    make_memory(description="활성")
    make_memory(description="죽은", archived_days_ago=5)
    results = memory_store.list_all(session, "team-test")
    descs = [m.description for m in results]
    assert "활성" in descs
    assert "죽은" not in descs


# ── untagged_alert ────────────────────────────────────────

def test_is_untagged_no_namespace_tags(session, make_memory):
    """source: 만 있으면 untagged로 본다."""
    from server.janitor import _is_untagged
    m = make_memory(tags=json.dumps(["source:declared"]))
    assert _is_untagged(m)


def test_is_untagged_with_project_tag(session, make_memory):
    from server.janitor import _is_untagged
    m = make_memory(tags=json.dumps(["project:billing", "source:declared"]))
    assert not _is_untagged(m)


def test_is_untagged_empty_tags(session, make_memory):
    from server.janitor import _is_untagged
    m = make_memory(tags="[]")
    assert _is_untagged(m)


def test_is_untagged_malformed_json(session, make_memory):
    from server.janitor import _is_untagged
    m = make_memory(tags="not-json{{{")
    assert _is_untagged(m)


def test_untagged_alert_warns_on_recent_burst(session, make_memory, caplog):
    """지난 7일 untagged 신규가 임계 초과 시 WARNING."""
    from server import janitor
    import logging
    janitor.log.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger="janitor")
    # 임계 +5건 신규 untagged
    for _ in range(janitor.UNTAGGED_WEEKLY_THRESHOLD + 5):
        make_memory(tags="[]", age_days=2)
    # 분류된 메모리 몇 개
    for _ in range(10):
        make_memory(tags=json.dumps(["domain:billing"]))
    janitor.untagged_alert()
    assert any("capture leakage" in r.message for r in caplog.records)


def test_untagged_alert_silent_when_below_threshold(session, make_memory, caplog):
    from server import janitor
    import logging
    janitor.log.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger="janitor")
    # 신규 untagged 5건만 (임계 30 미만)
    for _ in range(5):
        make_memory(tags="[]", age_days=2)
    janitor.untagged_alert()
    assert not any("capture leakage" in r.message for r in caplog.records)


def test_untagged_alert_excludes_archived(session, make_memory, caplog):
    """archived는 active 풀에서 빠져 untagged 집계에 안 잡힘 — 살아있는 untagged 1건만 포함."""
    from server import janitor
    import logging
    janitor.log.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="janitor")
    for _ in range(50):
        make_memory(tags="[]", archived_days_ago=10)   # archived → active 아님
    make_memory(tags="[]", age_days=2)                 # active untagged 1건
    make_memory(tags=json.dumps(["domain:x"]))         # active 분류된 1건
    janitor.untagged_alert()
    info_msgs = [r.message for r in caplog.records if "[untagged_alert]" in r.message]
    # active 2건 중 1건만 untagged
    assert any("total_untagged=1/2" in m for m in info_msgs)


# ── register ──────────────────────────────────────────────

def test_register_adds_seven_jobs(session):
    from apscheduler.schedulers.background import BackgroundScheduler
    from server import janitor
    sched = BackgroundScheduler(timezone="Asia/Seoul")
    janitor.register(sched)
    job_ids = {j.id for j in sched.get_jobs()}
    expected = {
        "janitor_snapshot", "janitor_decay",
        "janitor_soft_archive", "janitor_dup_scan", "janitor_tag_skew",
        "janitor_untagged_alert", "janitor_hard_delete",
    }
    assert expected.issubset(job_ids)


def test_register_respects_disabled_flag(session, monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler
    from server import janitor
    monkeypatch.setenv("JANITOR_ENABLED", "false")
    sched = BackgroundScheduler(timezone="Asia/Seoul")
    janitor.register(sched)
    assert not [j for j in sched.get_jobs() if j.id.startswith("janitor_")]
