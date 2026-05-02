"""LLM 토큰 사용량 기록."""

from __future__ import annotations

from sqlalchemy.orm import Session

from server.db import TokenUsage, engine


def record(
    team_id: str,
    team_name: str,
    member_name: str,
    provider: str,
    model: str,
    operation: str,
    prompt_tokens: int,
    completion_tokens: int,
):
    try:
        with Session(engine) as session:
            usage = TokenUsage(
                team_id=team_id,
                team_name=team_name,
                member_name=member_name,
                provider=provider,
                model=model,
                operation=operation,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            session.add(usage)
            session.commit()
    except Exception:
        pass
