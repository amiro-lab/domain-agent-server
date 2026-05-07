"""Capture transcript의 토픽 경계 식별 + chunk 분할.

목적: 8시간짜리 세션을 한 LLM 호출에 던지면 주제 흐려진 평균 추출이 됨.
주제 변경 지점을 LLM이 식별 → chunk별 analyzer.analyze 호출 → fact/preference/ontology
가 의미 단위로 정확하게 추출되도록.

session_id는 transcript-level uuid + chunk index 형식으로 합성됨:
  abc123_w00, abc123_w01, ...
이렇게 해서 siblings API가 chunk 단위로 동작.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("server.chunker")

# 분할 안 할 임계 — 짧은 세션은 그대로
MIN_MESSAGES_FOR_SPLIT = 10
MIN_MESSAGES_PER_CHUNK = 4
MAX_CHUNKS = 12


def _parse_messages(transcript_text: str) -> list[str]:
    """flatten된 transcript를 메시지 단위 split.

    `_flatten_transcript`가 만드는 포맷: 각 메시지가 `\\n\\n`로 구분되고
    `[user] ...`, `[assistant] ...`, `[tool] ...` 줄로 시작.
    """
    if not transcript_text:
        return []
    parts = re.split(r"\n\n(?=\[(?:user|assistant|tool)\])", transcript_text.strip())
    return [p.strip() for p in parts if p.strip()]


def _parse_chunks_response(raw: str, max_idx: int) -> list[dict]:
    """LLM 응답에서 chunks 배열 파싱. 마크다운 코드블록 제거."""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0].strip()
    if "{" in s and "}" in s:
        s = s[s.index("{"):s.rindex("}") + 1]
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return []
    chunks = obj.get("chunks") if isinstance(obj, dict) else None
    if not isinstance(chunks, list):
        return []
    cleaned: list[dict] = []
    for c in chunks:
        if not isinstance(c, dict):
            continue
        try:
            start = int(c.get("start"))
        except (TypeError, ValueError):
            continue
        if start < 0 or start >= max_idx:
            continue
        label = (c.get("label") or "chunk").strip()[:80]
        cleaned.append({"start": start, "label": label})
    # start 단조 증가, 중복 제거
    cleaned.sort(key=lambda c: c["start"])
    dedup: list[dict] = []
    for c in cleaned:
        if not dedup or c["start"] > dedup[-1]["start"]:
            dedup.append(c)
    if dedup and dedup[0]["start"] != 0:
        # 첫 chunk는 start=0이어야 함
        dedup.insert(0, {"start": 0, "label": "도입"})
    return dedup


def _enforce_min_size(chunks: list[dict], total_msgs: int, min_size: int) -> list[dict]:
    """너무 작은 chunk를 옆 chunk와 합침. 최종 결과의 단조 증가 유지."""
    if not chunks:
        return []
    sizes = [
        (chunks[i + 1]["start"] if i + 1 < len(chunks) else total_msgs) - chunks[i]["start"]
        for i in range(len(chunks))
    ]
    merged: list[dict] = []
    for c, sz in zip(chunks, sizes):
        if sz < min_size and merged:
            # 이전 chunk에 흡수 — 라벨은 이전 라벨 유지
            continue
        merged.append(c)
    return merged


def split_by_topic(
    transcript_text: str,
    *,
    llm_summarize=None,
    ctx: dict | None = None,
    min_messages_for_split: int = MIN_MESSAGES_FOR_SPLIT,
    min_messages_per_chunk: int = MIN_MESSAGES_PER_CHUNK,
    max_chunks: int = MAX_CHUNKS,
) -> list[dict]:
    """transcript를 LLM 추론 토픽 경계로 분할.

    반환: [{"label": str, "text": str, "start": int, "end": int}, ...]
    LLM 실패·짧은 세션·파싱 오류는 모두 단일 chunk로 fallback (안전).
    """
    msgs = _parse_messages(transcript_text)
    n = len(msgs)
    if n < min_messages_for_split:
        return [{
            "label": "전체",
            "text": transcript_text,
            "start": 0,
            "end": n,
        }]

    # LLM 호출자 — 기본은 reporter._llm_summarize (lazy import로 순환참조 회피)
    if llm_summarize is None:
        from server.reporter import _llm_summarize
        llm_summarize = _llm_summarize

    numbered = "\n\n".join(f"[#{i}] {m}" for i, m in enumerate(msgs))
    prompt = (
        "다음 Claude Code transcript에서 주제(토픽) 경계를 찾아라.\n\n"
        "원칙:\n"
        f"- 명확한 주제 변경만 (미세한 화제 전환은 무시)\n"
        f"- 각 chunk는 최소 {min_messages_per_chunk}개 메시지\n"
        f"- 최대 {max_chunks}개 chunk\n"
        "- 첫 chunk는 start=0\n"
        "- start 인덱스는 단조 증가\n"
        "- label은 한국어 한 줄 (10~30자, '~~ 작업', '~~ 디버깅', '~~ 결정' 같은 명사구)\n\n"
        "응답은 단일 JSON 객체만. 마크다운 코드블록 금지:\n"
        '{"chunks": [{"start": 0, "label": "초기 셋업"}, {"start": 12, "label": "X 디버깅"}, ...]}\n\n'
        f"메시지 {n}개:\n\n{numbered}"
    )
    try:
        raw = llm_summarize(prompt, "topic_chunker", ctx or {})
    except Exception as e:
        log.warning(f"[chunker] LLM call failed: {e!r} — fallback single chunk")
        raw = ""

    parsed = _parse_chunks_response(raw, max_idx=n)
    if not parsed:
        return [{"label": "전체", "text": transcript_text, "start": 0, "end": n}]

    parsed = _enforce_min_size(parsed, n, min_messages_per_chunk)
    if len(parsed) > max_chunks:
        parsed = parsed[:max_chunks]

    out: list[dict] = []
    for i, c in enumerate(parsed):
        end = parsed[i + 1]["start"] if i + 1 < len(parsed) else n
        chunk_text = "\n\n".join(msgs[c["start"]:end])
        out.append({
            "label": c["label"],
            "text": chunk_text,
            "start": c["start"],
            "end": end,
        })
    return out
