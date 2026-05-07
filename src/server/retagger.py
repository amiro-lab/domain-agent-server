"""Untagged 메모리 백필: namespace 태그 없는 메모리에 LLM이 분류 태그 부여.

1회성 정리 도구. capture 결함으로 도메인 분류가 안 된 과거 메모리를 retag.
ongoing 작업은 capture prompt 강화로 해결해야 하므로 cron 등록 X — 수동 호출만.

호출 경로:
    POST /member/memory/retag-untagged?dry_run=true&limit=20
    POST /member/memory/retag-untagged?dry_run=false&limit=200
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from server.db import Memory
from server.janitor import _is_untagged
from server.reporter import _llm_summarize

log = logging.getLogger("janitor")  # janitor 로그에 함께 누적

BATCH_SIZE = 5         # LLM 한 번에 보낼 메모리 수
MAX_TAGS_PER_ITEM = 3  # LLM이 메모리당 제안할 최대 태그
PROMPT = """다음 메모리들에 적절한 namespace 태그를 부여해줘.

태그 규칙:
- `project:<이름>` (특정 프로젝트, 예: project:domain-agent, project:billing)
- `domain:<이름>` (업무 도메인, 예: domain:auth, domain:checkout)
- `team:<이름>` (조직, 예: team:infra)
- `tech:<이름>` (기술 스택, 예: tech:fastapi, tech:sqlite)
- 메모리당 최대 3개. 명확한 분류가 어려우면 1~2개만.
- 새 태그 만들지 말고 일반적인 영문 소문자+하이픈 사용.

출력 형식: 메모리 ID와 태그를 한 줄씩.
`<memory_id>: tag1, tag2, tag3`

판단 어려우면 그 ID를 출력에서 빼라 (스킵 처리됨).

---

"""


def _parse_proposals(raw: str) -> dict[str, list[str]]:
    """LLM 출력에서 {memory_id: [tag, ...]} 추출."""
    proposals: dict[str, list[str]] = {}
    pattern = re.compile(r"^\s*[-*]?\s*`?([A-Za-z0-9_-]+(?:_[A-Za-z0-9_-]+)+)`?\s*[:：]\s*(.+?)\s*$")
    for line in raw.splitlines():
        m = pattern.match(line.strip().lstrip("-*•").strip())
        if not m:
            continue
        mem_id = m.group(1).strip()
        tag_str = m.group(2).strip().strip("`")
        tags = [
            t.strip().strip("`").strip().lower()
            for t in re.split(r"[,，;]", tag_str)
            if t.strip()
        ]
        # namespace 태그만 인정
        valid = [t for t in tags if ":" in t and not t.startswith("source:")]
        if valid:
            proposals[mem_id] = valid[:MAX_TAGS_PER_ITEM]
    return proposals


def _format_batch(batch: list[Memory]) -> str:
    lines = []
    for m in batch:
        body = (m.content or "").strip().replace("\n", " ")[:200]
        lines.append(f"### `{m.id}` (type={m.mem_type})\n설명: {m.description}\n본문: {body}")
    return "\n\n".join(lines)


def retag_untagged(
    session: Session,
    team_id: str,
    dry_run: bool = True,
    limit: int | None = None,
) -> dict:
    """untagged 메모리에 LLM이 namespace 태그 제안.

    dry_run=True: 제안만 반환, DB 미변경
    dry_run=False: 실제 tags 컬럼 업데이트
    limit=None: 전체 untagged 처리 (운영 주의)
    """
    active = (
        session.query(Memory)
        .filter_by(team_id=team_id)
        .filter(Memory.archived_at.is_(None))
        .all()
    )
    untagged = [m for m in active if _is_untagged(m)]
    targets = untagged if limit is None else untagged[:limit]

    if not targets:
        return {
            "untagged_total": 0,
            "processed": 0,
            "applied": 0,
            "dry_run": dry_run,
            "preview": [],
        }

    all_proposals: dict[str, list[str]] = {}
    for i in range(0, len(targets), BATCH_SIZE):
        batch = targets[i:i + BATCH_SIZE]
        prompt = PROMPT + _format_batch(batch)
        try:
            raw = _llm_summarize(prompt, "retag_untagged", {"team_id": team_id})
        except Exception as e:
            log.error(f"[retagger] LLM call failed batch={i}: {e}")
            continue
        all_proposals.update(_parse_proposals(raw))

    applied = 0
    preview = []
    by_id = {m.id: m for m in targets}
    for mem_id, new_tags in all_proposals.items():
        mem = by_id.get(mem_id)
        if not mem:
            continue
        try:
            existing = json.loads(mem.tags) if mem.tags else []
        except (json.JSONDecodeError, TypeError):
            existing = []
        merged = list(dict.fromkeys(existing + new_tags))   # 순서 유지 dedupe

        preview.append({
            "id": mem.id,
            "description": mem.description[:100],
            "old_tags": existing,
            "new_tags": new_tags,
            "merged": merged,
        })

        if not dry_run:
            mem.tags = json.dumps(merged, ensure_ascii=False)
            applied += 1

    if not dry_run:
        session.commit()

    log.info(
        f"[retagger] team={team_id} dry_run={dry_run} "
        f"untagged_total={len(untagged)} processed={len(targets)} "
        f"proposed={len(all_proposals)} applied={applied}"
    )

    return {
        "untagged_total": len(untagged),
        "processed": len(targets),
        "proposed": len(all_proposals),
        "applied": applied,
        "dry_run": dry_run,
        "preview": preview[:20],   # 응답 부피 제한
    }
