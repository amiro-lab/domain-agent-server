"""DB 설정 및 모델."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.pool import NullPool

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./domain_agent.db")

_is_sqlite = DATABASE_URL.startswith("sqlite")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    poolclass=NullPool if _is_sqlite else None,
)


def get_session():
    with Session(engine) as session:
        yield session


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ProviderSettings(Base):
    __tablename__ = "provider_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    provider: Mapped[str] = mapped_column(String(20), default="openai")
    openai_api_key: Mapped[str] = mapped_column(Text, default="")
    openai_model: Mapped[str] = mapped_column(String(50), default="gpt-4o-mini")
    anthropic_api_key: Mapped[str] = mapped_column(Text, default="")
    anthropic_model: Mapped[str] = mapped_column(String(50), default="claude-haiku-4-5-20251001")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    join_code: Mapped[str] = mapped_column(String(20), default="")
    memory_limit: Mapped[int] = mapped_column(Integer, default=0)  # 0 = 무제한
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    api_keys: Mapped[list[APIKey]] = relationship("APIKey", back_populates="team")
    memories: Mapped[list[Memory]] = relationship("Memory", back_populates="team")


class APIKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(100), default="default")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    team: Mapped[Team] = relationship("Team", back_populates="api_keys")


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (UniqueConstraint("team_id", "description_hash"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), nullable=False)
    mem_type: Mapped[str] = mapped_column(String(20))
    description: Mapped[str] = mapped_column(String(500))
    description_hash: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.7)
    tags: Mapped[str] = mapped_column(Text, default="")
    source_platform: Mapped[str] = mapped_column(String(50), default="")
    captured_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    team: Mapped[Team] = relationship("Team", back_populates="memories")


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), nullable=False)
    report_type: Mapped[str] = mapped_column(String(20))
    period: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    new_memory_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class TeamMember(Base):
    __tablename__ = "team_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(200), default="")
    api_key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id"), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_active: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class WeeklySchedule(Base):
    __tablename__ = "weekly_schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), nullable=False, unique=True)
    day_of_week: Mapped[int] = mapped_column(default=0)
    hour: Mapped[int] = mapped_column(default=9)
    minute: Mapped[int] = mapped_column(default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class TokenUsage(Base):
    __tablename__ = "token_usages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    team_id: Mapped[str] = mapped_column(String(36), nullable=True)
    team_name: Mapped[str] = mapped_column(String(200), default="")
    member_name: Mapped[str] = mapped_column(String(100), default="")
    provider: Mapped[str] = mapped_column(String(20), default="")
    model: Mapped[str] = mapped_column(String(50), default="")
    operation: Mapped[str] = mapped_column(String(50), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DomainSummary(Base):
    __tablename__ = "domain_summaries"
    __table_args__ = (UniqueConstraint("team_id", "tag"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    team_id: Mapped[str] = mapped_column(String(36), nullable=False)
    tag: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    memory_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    team_id: Mapped[str] = mapped_column(String(36), default="")
    team_name: Mapped[str] = mapped_column(String(200), default="")
    member_name: Mapped[str] = mapped_column(String(100), default="")
    method: Mapped[str] = mapped_column(String(10), default="")
    endpoint: Mapped[str] = mapped_column(String(200), default="")
    status_code: Mapped[int] = mapped_column(Integer, default=200)
    ip_address: Mapped[str] = mapped_column(String(50), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


def create_tables():
    Base.metadata.create_all(engine)
    _migrate_columns()


def _migrate_columns():
    """SQLite ALTER TABLE for missing columns."""
    from sqlalchemy import inspect, text
    inspector = inspect(engine)

    migrations = {
        "teams": [
            ("memory_limit", "INTEGER DEFAULT 0"),
            ("enabled", "BOOLEAN DEFAULT 1"),
        ],
        "api_keys": [
            ("enabled", "BOOLEAN DEFAULT 1"),
            ("last_used_at", "DATETIME"),
        ],
        "team_members": [
            ("enabled", "BOOLEAN DEFAULT 1"),
        ],
        "memories": [
            ("captured_by", "VARCHAR(100) DEFAULT ''"),
            ("last_verified_at", "DATETIME"),
            ("archived_at", "DATETIME"),
        ],
    }

    with engine.connect() as conn:
        for table, cols in migrations.items():
            if table not in inspector.get_table_names():
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for col_name, col_def in cols:
                if col_name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}"))
        conn.commit()
