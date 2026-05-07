"""테스트 픽스처: 임시 SQLite DB + janitor 모듈 설정 격리."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_env(tmp_path_factory):
    """모든 테스트 전에 DATABASE_URL과 janitor 경로를 tmpdir로 격리."""
    base = tmp_path_factory.mktemp("dataroot")
    db_path = base / "test.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["JANITOR_LOG_PATH"] = str(base / "janitor.log")
    os.environ["JANITOR_BACKUP_DIR"] = str(base / "backups")
    os.environ["JANITOR_DRY_RUN"] = "false"
    yield
    # tmp_path_factory가 알아서 정리


@pytest.fixture()
def session(_isolate_env):
    """각 테스트마다 깨끗한 DB 세션."""
    from server.db import create_tables, engine, Team
    from sqlalchemy.orm import Session as SQLSession

    create_tables()

    with SQLSession(engine) as s:
        # 모든 테이블 비우기 (외래키 순서 고려)
        from server.db import Memory, APIKey, TeamMember, AuditLog, TokenUsage
        for model in (Memory, APIKey, TeamMember, AuditLog, TokenUsage):
            s.query(model).delete()
        s.query(Team).delete()
        s.commit()

        s.add(Team(id="team-test", name="test", created_at=datetime.now(timezone.utc)))
        s.add(Team(id="team-other", name="other", created_at=datetime.now(timezone.utc)))
        s.commit()
        yield s


@pytest.fixture()
def make_memory(session):
    """Memory 인스턴스 빠르게 생성하는 헬퍼.

    description_hash는 memory_store._desc_hash와 동일한 알고리즘을 사용해
    save() 호출 시 upsert가 정상 매칭되게 한다.
    """
    from server.db import Memory
    from server.memory_store import _desc_hash
    counter = {"i": 0}

    def _make(
        confidence: float = 0.7,
        age_days: int = 0,           # last_verified_at = now - age_days
        archived_days_ago: int | None = None,
        description: str | None = None,
        tags: str = "[]",
        mem_type: str = "fact",
        team_id: str = "team-test",
    ) -> Memory:
        counter["i"] += 1
        i = counter["i"]
        now = datetime.now(timezone.utc)
        verified = now - timedelta(days=age_days)
        archived = None if archived_days_ago is None else now - timedelta(days=archived_days_ago)
        desc = description or f"test memory {i}"
        m = Memory(
            id=f"fact_test_{i:04d}",
            team_id=team_id,
            mem_type=mem_type,
            description=desc,
            description_hash=_desc_hash(desc),
            content=f"content {i}",
            confidence=confidence,
            tags=tags,
            source_platform="test",
            captured_by="pytest",
            created_at=verified,
            updated_at=verified,
            last_verified_at=verified,
            archived_at=archived,
        )
        session.add(m)
        session.commit()
        return m

    return _make


@pytest.fixture()
def relaxed_cap(monkeypatch):
    """cap 가드를 사실상 비활성 (테스트용 작은 표본 허용)."""
    from server import janitor
    monkeypatch.setattr(janitor, "DAILY_CAP_RATIO", 1.0)
    monkeypatch.setattr(janitor, "_within_cap", lambda *a, **kw: True)
