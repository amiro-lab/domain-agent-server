"""메모리 목록을 자연어 요약으로 압축. 결과를 캐시해 LLM 호출 최소화."""

from __future__ import annotations

import hashlib
import json
import os
import time

_cache: dict[str, tuple[str, str, float]] = {}
CACHE_TTL = 600


def _content_hash(memories: list[dict]) -> str:
    ids = sorted(m.get("id", "") for m in memories)
    return hashlib.md5(json.dumps(ids).encode()).hexdigest()[:12]


def _compress_anthropic(memories: list[dict], max_chars: int, ctx: dict | None = None) -> str:
    import anthropic
    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    facts = "\n".join(f"- {m['description']}" for m in memories)
    msg = client.messages.create(
        model=model,
        max_tokens=300,
        messages=[{"role": "user", "content":
            f"다음 팀 도메인 지식을 {max_chars}자 이내의 자연스러운 한국어 문단으로 요약하라. "
            f"불릿 리스트 금지. 핵심만.\n\n{facts}"}],
    )
    if ctx:
        from server import token_tracker
        usage = msg.usage
        token_tracker.record(
            ctx.get("team_id", ""), ctx.get("team_name", ""), ctx.get("member_name", ""),
            "anthropic", model, "compress",
            usage.input_tokens, usage.output_tokens,
        )
    return msg.content[0].text.strip()


def _compress_openai(memories: list[dict], max_chars: int, ctx: dict | None = None) -> str:
    import openai
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    facts = "\n".join(f"- {m['description']}" for m in memories)
    resp = client.chat.completions.create(
        model=model,
        max_tokens=300,
        messages=[
            {"role": "system", "content": "팀 도메인 지식을 자연스러운 한국어 문단으로 압축하는 도우미."},
            {"role": "user", "content":
                f"다음 팀 지식을 {max_chars}자 이내 문단으로 요약하라. 불릿 금지.\n\n{facts}"},
        ],
    )
    if ctx:
        from server import token_tracker
        usage = resp.usage
        token_tracker.record(
            ctx.get("team_id", ""), ctx.get("team_name", ""), ctx.get("member_name", ""),
            "openai", model, "compress",
            usage.prompt_tokens, usage.completion_tokens,
        )
    return resp.choices[0].message.content.strip()


def get_brief(team_id: str, memories: list[dict], max_chars: int = 500, ctx: dict | None = None) -> str:
    if not memories:
        return ""

    if len(memories) <= 5:
        return ". ".join(m["description"].rstrip(".") for m in memories)[:max_chars]

    c_hash = _content_hash(memories)
    cached = _cache.get(team_id)
    if cached and cached[0] == c_hash and time.time() - cached[2] < CACHE_TTL:
        return cached[1]

    try:
        provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
        summary = _compress_openai(memories, max_chars, ctx) if provider == "openai" \
            else _compress_anthropic(memories, max_chars, ctx)
    except Exception:
        summary = ". ".join(m["description"].rstrip(".") for m in memories[:7])

    summary = summary[:max_chars]
    _cache[team_id] = (c_hash, summary, time.time())
    return summary
