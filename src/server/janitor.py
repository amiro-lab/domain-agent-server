"""메모리 자동 정리 (cron jobs).

3단 깔때기:
  1) confidence_decay  — 매일, 가만 두면 신뢰도 자연 감쇠
  2) soft_archive      — 주간, confidence < threshold면 archived_at 마킹
  3) hard_delete       — 월간, archived_at 90일 경과한 것만 영구 삭제

안전장치:
  - DRY_RUN=true면 변경 없이 로그만
  - 한 번에 전체의 DAILY_CAP_RATIO 초과 변동 시 자동 중단
  - 모든 작업 결과는 /data/janitor.log에 누적
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from server.db import Memory, engine

LOG_PATH = Path(os.environ.get("JANITOR_LOG_PATH", "/data/janitor.log"))
BACKUP_DIR = Path(os.environ.get("JANITOR_BACKUP_DIR", "/data/backups"))
DB_PATH = Path("/data/domain_agent.db")

DRY_RUN = os.environ.get("JANITOR_DRY_RUN", "true").lower() == "true"
DAILY_CAP_RATIO = float(os.environ.get("JANITOR_CAP_RATIO", "0.05"))

DECAY_GRACE_DAYS = 30        # 30일까지 무감쇠
DECAY_PER_DAY = 0.005        # 그 이후 하루 -0.005 (60일=−0.15, 90일=−0.30)
ARCHIVE_THRESHOLD = 0.30     # 이 미만이면 soft archive
HARD_DELETE_DAYS = 90        # archived_at 후 N일 → 영구 삭제
BACKUP_RETENTION = 14        # daily 스냅샷 14일치
UNTAGGED_WEEKLY_THRESHOLD = 30   # 지난 7일 untagged 신규 이게 넘으면 capture 점검 알람


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("janitor")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(LOG_PATH)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)
        logger.warning(f"janitor log path {LOG_PATH} not writable, falling back to stderr")
    return logger


log = _setup_logger()


def _ref_time(m: Memory) -> datetime:
    """감쇠 기준 시각: last_verified_at > updated_at > created_at."""
    t = m.last_verified_at or m.updated_at or m.created_at
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t


def _within_cap(changed: int, total: int, job: str) -> bool:
    """하루 변동 cap 가드 — 전체의 5% 초과 시 거부."""
    if total == 0:
        return True
    ratio = changed / total
    if ratio > DAILY_CAP_RATIO:
        log.error(f"[{job}] CAP EXCEEDED ratio={ratio:.3f} changed={changed} total={total} — aborting")
        return False
    return True


# ── DAILY ─────────────────────────────────────────────────

def snapshot():
    """SQLite DB 파일을 backups/로 복사. 14일치 rotation."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists():
        log.warning(f"[snapshot] {DB_PATH} not found, skip")
        return

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    dst = BACKUP_DIR / f"domain_agent_{today}.db"
    if DRY_RUN:
        log.info(f"[snapshot] DRY_RUN would copy {DB_PATH} → {dst}")
    else:
        shutil.copy2(DB_PATH, dst)
        log.info(f"[snapshot] {dst.name} ({dst.stat().st_size} bytes)")

    cutoff = datetime.now(timezone.utc) - timedelta(days=BACKUP_RETENTION)
    removed = 0
    for f in BACKUP_DIR.glob("domain_agent_*.db"):
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            if DRY_RUN:
                log.info(f"[snapshot] DRY_RUN would remove old backup {f.name}")
            else:
                f.unlink()
            removed += 1
    if removed:
        log.info(f"[snapshot] rotated out {removed} old backups")


def confidence_decay():
    """grace 후 하루 -0.005씩 감쇠. last_verified_at 기준."""
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        active = session.query(Memory).filter(Memory.archived_at.is_(None)).all()
        total = len(active)
        changed = 0
        for m in active:
            days = (now - _ref_time(m)).days
            if days <= DECAY_GRACE_DAYS:
                continue
            decay = (days - DECAY_GRACE_DAYS) * DECAY_PER_DAY
            new_conf = max(0.0, m.confidence - decay)
            if abs(new_conf - m.confidence) < 1e-6:
                continue
            if not DRY_RUN:
                m.confidence = new_conf
            changed += 1
        if not DRY_RUN:
            session.commit()
        log.info(f"[confidence_decay] dry_run={DRY_RUN} total={total} changed={changed}")


# ── WEEKLY ────────────────────────────────────────────────

def soft_archive():
    """confidence < 0.3 → archived_at 기록 (소프트 삭제)."""
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        active = session.query(Memory).filter(Memory.archived_at.is_(None)).all()
        total = len(active)
        targets = [m for m in active if m.confidence < ARCHIVE_THRESHOLD]
        if not _within_cap(len(targets), total, "soft_archive"):
            return
        if not DRY_RUN:
            for m in targets:
                m.archived_at = now
            session.commit()
        sample = [m.id for m in targets[:5]]
        log.info(f"[soft_archive] dry_run={DRY_RUN} total={total} archived={len(targets)} sample={sample}")


def _dup_clusters(active: list[Memory], threshold: float) -> list[list[Memory]]:
    """team_id + mem_type 같은 항목끼리 Jaccard ≥ threshold로 union-find 클러스터링.

    PROTECTED 태그가 있는 항목은 자체로 별 클러스터(스스로 1개)로 두어 머지 대상에서 제외.
    """
    from server.memory_store import _jaccard, _parse_tags, _tokens, has_protected_tag

    # team_id+mem_type별로 분할 — cross-bucket 비교 안 함
    buckets: dict[tuple[str, str], list[Memory]] = {}
    for m in active:
        buckets.setdefault((m.team_id, m.mem_type), []).append(m)

    clusters: list[list[Memory]] = []
    for items in buckets.values():
        # 인덱스 union-find
        parent = list(range(len(items)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        token_cache = [_tokens(m.description) for m in items]
        protected = [has_protected_tag(_parse_tags(m.tags)) for m in items]

        for i in range(len(items)):
            if protected[i]:
                continue
            for j in range(i + 1, len(items)):
                if protected[j]:
                    continue
                if _jaccard(token_cache[i], token_cache[j]) >= threshold:
                    union(i, j)

        groups: dict[int, list[Memory]] = {}
        for i, m in enumerate(items):
            if protected[i]:
                continue
            groups.setdefault(find(i), []).append(m)
        for g in groups.values():
            if len(g) > 1:
                clusters.append(g)
    return clusters


def _pick_canonical(group: list[Memory]) -> Memory:
    """그룹 내 canonical: 최고 confidence → 가장 최근 last_verified → id 안정 정렬."""
    return sorted(
        group,
        key=lambda m: (-m.confidence, -(_ref_time(m).timestamp()), m.id),
    )[0]


def dup_scan():
    """동일 team+mem_type 내에서 description Jaccard ≥ 0.6 인 클러스터 자동 머지.

    각 클러스터에서 canonical 1개에 confidence=max, tags=union, last_verified=now으로 bump.
    나머지는 soft archive (이후 hard_delete가 90일 후 영구 정리).
    PROTECTED 태그(v*_fixed)는 의미 보존 위해 제외.
    """
    from server.memory_store import FUZZY_MERGE_THRESHOLD, _parse_tags, _union_tags

    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        active = session.query(Memory).filter(Memory.archived_at.is_(None)).all()
        total = len(active)
        clusters = _dup_clusters(active, FUZZY_MERGE_THRESHOLD)

        merged_count = 0  # canonical에 합쳐 archive된 항목 수
        canonicals: list[str] = []
        for group in clusters:
            canonical = _pick_canonical(group)
            others = [m for m in group if m.id != canonical.id]

            new_conf = canonical.confidence
            new_tags = _parse_tags(canonical.tags)
            for m in others:
                new_conf = max(new_conf, m.confidence)
                new_tags = _union_tags(new_tags, _parse_tags(m.tags))
            new_conf = min(0.95, new_conf)

            if not DRY_RUN:
                canonical.confidence = new_conf
                canonical.tags = json.dumps(new_tags, ensure_ascii=False)
                canonical.last_verified_at = now
                for m in others:
                    m.archived_at = now
            merged_count += len(others)
            canonicals.append(canonical.id)

        # 영향 범위가 너무 크면 (전체의 5% 초과) 거부 — 안전장치
        if not _within_cap(merged_count, total, "dup_scan"):
            session.rollback()
            return

        if not DRY_RUN:
            session.commit()

        log.info(
            f"[dup_scan] dry_run={DRY_RUN} active={total} clusters={len(clusters)} "
            f"merged_into_canonical={merged_count} canonical_sample={canonicals[:5]}"
        )


def tag_skew_alert():
    """단일 태그 비중 > 20% 시 알림."""
    with Session(engine) as session:
        active = session.query(Memory).filter(Memory.archived_at.is_(None)).all()
        total = len(active)
        if total == 0:
            return
        tag_counter = Counter()
        for m in active:
            try:
                tags = json.loads(m.tags) if m.tags else []
            except json.JSONDecodeError:
                tags = []
            tag_counter.update(tags)
        for tag, count in tag_counter.most_common(5):
            ratio = count / total
            if ratio > 0.20:
                log.warning(f"[tag_skew] tag={tag!r} count={count} ratio={ratio:.1%} (>20%)")


def _is_untagged(m: Memory) -> bool:
    """namespace 태그(project:/domain:/team:/tech:/system: 등) 부재 = untagged.

    source: 태그는 자동 부여되는 메타라 분류로 안 침. 분류 효과 있는 태그가 0개면 untagged.
    """
    try:
        tags = json.loads(m.tags) if m.tags else []
    except (json.JSONDecodeError, TypeError):
        return True
    return not any(":" in t and not t.startswith("source:") for t in tags)


def untagged_alert():
    """namespace 태그 없는 untagged 메모리 누적/신규 카운트.

    LLM 호출 0 — capture가 태그를 충분히 못 뽑고 있는지 가시화하는 진단 전용.
    임계 초과 시 WARNING 로그 → capture prompt 점검 신호.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    with Session(engine) as session:
        active = session.query(Memory).filter(Memory.archived_at.is_(None)).all()
        total = len(active)
        if total == 0:
            return
        untagged = [m for m in active if _is_untagged(m)]
        recent_untagged = [
            m for m in untagged
            if (m.created_at or m.updated_at) and _ref_time(m) >= cutoff
        ]
        ratio = len(untagged) / total
        log.info(
            f"[untagged_alert] total_untagged={len(untagged)}/{total} ({ratio:.1%}), "
            f"recent_7d={len(recent_untagged)}"
        )
        if len(recent_untagged) > UNTAGGED_WEEKLY_THRESHOLD:
            log.warning(
                f"[untagged_alert] capture leakage 의심 — 지난 7일 untagged "
                f"{len(recent_untagged)}건 (임계 {UNTAGGED_WEEKLY_THRESHOLD}). capture 단 점검 권장."
            )


# ── MONTHLY ───────────────────────────────────────────────

def hard_delete():
    """archived_at + HARD_DELETE_DAYS 경과한 항목 영구 삭제."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=HARD_DELETE_DAYS)
    with Session(engine) as session:
        targets = session.query(Memory).filter(
            Memory.archived_at.isnot(None),
            Memory.archived_at < cutoff,
        ).all()
        total = session.query(func.count(Memory.id)).scalar() or 0
        if not _within_cap(len(targets), total, "hard_delete"):
            return
        sample = [m.id for m in targets[:5]]
        if not DRY_RUN:
            for m in targets:
                session.delete(m)
            session.commit()
        log.info(f"[hard_delete] dry_run={DRY_RUN} deleted={len(targets)} cutoff={cutoff.isoformat()} sample={sample}")


# ── 등록 ──────────────────────────────────────────────────

def register(scheduler):
    """main.py의 BackgroundScheduler에 작업을 등록."""
    from apscheduler.triggers.cron import CronTrigger

    if os.environ.get("JANITOR_ENABLED", "true").lower() != "true":
        log.info("[register] JANITOR_ENABLED=false, skipping")
        return

    jobs = [
        ("janitor_snapshot",        snapshot,         CronTrigger(hour=3, minute=0)),
        ("janitor_decay",           confidence_decay, CronTrigger(hour=3, minute=5)),
        ("janitor_soft_archive",    soft_archive,     CronTrigger(day_of_week="mon", hour=3, minute=30)),
        ("janitor_dup_scan",        dup_scan,         CronTrigger(day_of_week="mon", hour=3, minute=40)),
        ("janitor_tag_skew",        tag_skew_alert,   CronTrigger(day_of_week="mon", hour=3, minute=50)),
        ("janitor_untagged_alert",  untagged_alert,   CronTrigger(day_of_week="mon", hour=3, minute=55)),
        ("janitor_hard_delete",     hard_delete,      CronTrigger(day=1, hour=4, minute=15)),
    ]
    for job_id, fn, trigger in jobs:
        scheduler.add_job(fn, trigger, id=job_id, replace_existing=True, max_instances=1)

    log.info(f"[register] {len(jobs)} jobs registered (DRY_RUN={DRY_RUN}, CAP={DAILY_CAP_RATIO})")
