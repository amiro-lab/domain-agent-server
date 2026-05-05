"""Memory.captured_by가 비어있는 행을 audit log + 단일멤버 휴리스틱으로 채운다.

매칭 우선순위:
1. AuditLog 매칭 — 같은 team, /api/capture or /api/memory POST(2xx),
   memory.created_at - 10분 ≤ audit.created_at ≤ memory.created_at + 1초.
   가장 가까운 비어있지 않은 member_name 사용.
2. 팀에 활성 멤버가 1명뿐이면 그 멤버 이름.
3. 그래도 없으면 그대로 둠.

사용법:
  python backfill_captured_by.py            # dry-run
  python backfill_captured_by.py --apply    # 실제 반영
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from sqlalchemy.orm import Session

from server.db import AuditLog, Memory, TeamMember, engine


CAPTURE_ENDPOINTS = ("/api/capture", "/api/memory")
PRE_WINDOW = timedelta(minutes=10)   # 캡처는 백그라운드라 메모리가 늦게 박힘
POST_WINDOW = timedelta(seconds=1)   # 시계 미세 차이 보정


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="실제 DB에 반영")
    args = ap.parse_args()

    with Session(engine) as session:
        empties = (
            session.query(Memory)
            .filter((Memory.captured_by == "") | (Memory.captured_by.is_(None)))
            .all()
        )
        total = session.query(Memory).count()
        print(f"전체 메모리: {total}, 미설정: {len(empties)}")

        if not empties:
            print("백필할 항목 없음.")
            return

        # 팀별 audit log 캐시
        team_logs: dict[str, list[AuditLog]] = defaultdict(list)
        for team_id in {m.team_id for m in empties}:
            logs = (
                session.query(AuditLog)
                .filter(
                    AuditLog.team_id == team_id,
                    AuditLog.endpoint.in_(CAPTURE_ENDPOINTS),
                    AuditLog.method == "POST",
                    AuditLog.status_code >= 200,
                    AuditLog.status_code < 300,
                    AuditLog.member_name != "",
                )
                .order_by(AuditLog.created_at.asc())
                .all()
            )
            team_logs[team_id] = logs
            print(f"  team={team_id}: 후보 audit log {len(logs)}건")

        # 팀별 단일 멤버 fallback
        team_solo: dict[str, str] = {}
        for team_id in {m.team_id for m in empties}:
            members = (
                session.query(TeamMember)
                .filter_by(team_id=team_id, enabled=True)
                .all()
            )
            if len(members) == 1:
                team_solo[team_id] = members[0].name

        plan: list[tuple[Memory, str, str]] = []  # (memory, name, source)
        unresolved: list[Memory] = []

        for mem in empties:
            logs = team_logs.get(mem.team_id, [])
            mc = mem.created_at
            best: str | None = None
            for log in reversed(logs):  # 최신부터
                lc = log.created_at
                if lc <= mc + POST_WINDOW and lc >= mc - PRE_WINDOW:
                    best = log.member_name
                    break
                if lc < mc - PRE_WINDOW:
                    break

            if best:
                plan.append((mem, best, "audit"))
            elif mem.team_id in team_solo:
                plan.append((mem, team_solo[mem.team_id], "solo"))
            else:
                unresolved.append(mem)

        # 통계
        by_name = Counter((src, name) for _, name, src in plan)
        print(f"\n매칭 결과: 채울 수 있음 {len(plan)} / 미해결 {len(unresolved)}")
        for (src, name), count in by_name.most_common():
            print(f"  [{src}] {name}: {count}건")

        if unresolved:
            print(f"\n미해결 샘플 (최대 5건):")
            for mem in unresolved[:5]:
                print(f"  team={mem.team_id} {mem.created_at.isoformat()} {mem.description[:60]}")

        if args.apply and plan:
            for mem, name, _ in plan:
                mem.captured_by = name
            session.commit()
            print(f"\n✓ {len(plan)}건 반영 완료.")
        elif plan:
            print("\n(dry-run — --apply 붙이면 실제 반영)")


if __name__ == "__main__":
    main()
