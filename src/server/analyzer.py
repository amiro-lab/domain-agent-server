"""LLM으로 대화/transcript 분석 → 메모리 추출."""

from __future__ import annotations

import json
import os
from datetime import date

SYSTEM = """\
당신은 업무 대화에서 팀 도메인 지식만 추출하는 메모리 큐레이터다.

## 추출 대상 (업무 관련)
- 사람·역할·책임 (누가 무엇을 담당하는지)
- 시스템·서비스·기술 스택
- 비즈니스 규칙·프로세스·의사결정
- 일정·마감·반복 이벤트
- 도메인 용어 정의

## 추출 제외 (반드시 무시)
- 음식·날씨·취미 등 개인 일상 잡담
- 감정 표현·안부 인사
- 업무와 무관한 개인 사생활
- 추출할 업무 지식이 없는 대화 전체

업무 무관 대화가 대부분이면 {{"extracted": []}} 반환.

응답은 반드시 단일 JSON 객체:
{{"extracted": [
  {{"type": "fact|preference|ontology", "description": "100자 이내 요약", "content": "본문", "confidence": 0.0-1.0, "tags": ["team:xxx", "domain:yyy"]}}
]}}

마크다운 코드블록 사용 금지.
태그 허용 네임스페이스: team:, system:, person:, domain:, tech:, source:
소문자+하이픈만, 항목당 최대 4개.

**태그 우선 사용 강제**: 아래 "이미 사용 중인 태그"에서 의미가 가장 가까운 것을 먼저 선택하라.
새 태그는 목록 어디에도 의미가 맞지 않을 때만 만든다. 동의어·표기 변형 금지 — `domain:billing` 이미 있으면 `domain:billings` / `project:billing` / `team:billing-pay` 같은 변형을 새로 만들지 말 것. 같은 주제는 같은 태그로.

이미 사용 중인 태그 (top-30 빈도순 — 우선 선택): {tag_summary}

오늘 날짜: {today}
"""


def _parse_json(raw: str) -> list[dict]:
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0].strip()
    if "{" in s and "}" in s:
        s = s[s.index("{"):s.rindex("}") + 1]
    try:
        return json.loads(s).get("extracted") or []
    except Exception:
        return []


def _analyze_anthropic(text: str, platform: str, ctx: dict | None = None) -> list[dict]:
    import anthropic
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return []
    client = anthropic.Anthropic(api_key=api_key)
    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    system = SYSTEM.format(
        today=date.today().isoformat(),
        tag_summary=(ctx or {}).get("tag_summary") or "(없음 — 자유 생성 가능)",
    )
    user_msg = f"다음 {platform} 대화에서 도메인 지식을 추출하라.\n\n---\n{text[:50000]}\n---"
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        if ctx:
            from server import token_tracker
            usage = msg.usage
            token_tracker.record(
                ctx.get("team_id", ""), ctx.get("team_name", ""), ctx.get("member_name", ""),
                "anthropic", model, "analyze",
                usage.input_tokens, usage.output_tokens,
            )
        return _parse_json(msg.content[0].text)
    except Exception:
        return []


def _analyze_openai(text: str, platform: str, ctx: dict | None = None) -> list[dict]:
    import openai
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return []
    client = openai.OpenAI(api_key=api_key)
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    system = SYSTEM.format(
        today=date.today().isoformat(),
        tag_summary=(ctx or {}).get("tag_summary") or "(없음 — 자유 생성 가능)",
    )
    user_msg = f"다음 {platform} 대화에서 도메인 지식을 추출하라.\n\n---\n{text[:50000]}\n---"
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=4096,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
        )
        if ctx:
            from server import token_tracker
            usage = resp.usage
            token_tracker.record(
                ctx.get("team_id", ""), ctx.get("team_name", ""), ctx.get("member_name", ""),
                "openai", model, "analyze",
                usage.prompt_tokens, usage.completion_tokens,
            )
        raw = resp.choices[0].message.content or ""
        return _parse_json(raw)
    except Exception:
        return []


def analyze(text: str, platform: str = "", ctx: dict | None = None) -> list[dict]:
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    if provider == "openai":
        return _analyze_openai(text, platform, ctx)
    return _analyze_anthropic(text, platform, ctx)
