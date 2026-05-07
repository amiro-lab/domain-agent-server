"""토픽 경계 chunker — _parse_messages, _parse_chunks_response, split_by_topic."""
from __future__ import annotations

import json
import pytest


# ── 메시지 split ──────────────────────────────────────────

def test_parse_messages_basic():
    from server.chunker import _parse_messages
    text = (
        "[user] 안녕\n\n"
        "[assistant] 반갑습니다\n\n"
        "[tool] Bash: ls\n\n"
        "[user] 다음 작업"
    )
    msgs = _parse_messages(text)
    assert len(msgs) == 4
    assert msgs[0].startswith("[user]")
    assert msgs[2].startswith("[tool]")


def test_parse_messages_empty():
    from server.chunker import _parse_messages
    assert _parse_messages("") == []
    assert _parse_messages(None) == []


def test_parse_messages_strips_whitespace():
    from server.chunker import _parse_messages
    # split regex는 \n\n 직후 [user|assistant|tool] 만 경계로 인식
    text = "  [user] X  \n\n[assistant] Y  "
    msgs = _parse_messages(text)
    assert msgs == ["[user] X", "[assistant] Y"]


# ── chunk 응답 파싱 ───────────────────────────────────────

def test_parse_chunks_response_valid():
    from server.chunker import _parse_chunks_response
    raw = '{"chunks":[{"start":0,"label":"도입"},{"start":5,"label":"디버깅"}]}'
    out = _parse_chunks_response(raw, max_idx=20)
    assert len(out) == 2
    assert out[0]["start"] == 0
    assert out[1]["start"] == 5


def test_parse_chunks_response_strips_markdown():
    from server.chunker import _parse_chunks_response
    raw = '```json\n{"chunks":[{"start":0,"label":"X"}]}\n```'
    out = _parse_chunks_response(raw, max_idx=10)
    assert len(out) == 1


def test_parse_chunks_response_drops_out_of_range():
    """max_idx 초과 start는 무시."""
    from server.chunker import _parse_chunks_response
    raw = '{"chunks":[{"start":0,"label":"a"},{"start":99,"label":"b"}]}'
    out = _parse_chunks_response(raw, max_idx=10)
    assert len(out) == 1


def test_parse_chunks_response_inserts_zero_start_if_missing():
    from server.chunker import _parse_chunks_response
    raw = '{"chunks":[{"start":3,"label":"중간시작"}]}'
    out = _parse_chunks_response(raw, max_idx=10)
    assert out[0]["start"] == 0
    assert any(c["start"] == 3 for c in out)


def test_parse_chunks_response_invalid_returns_empty():
    from server.chunker import _parse_chunks_response
    assert _parse_chunks_response("not json", 10) == []
    assert _parse_chunks_response("", 10) == []


# ── split_by_topic 통합 ───────────────────────────────────

def test_split_short_transcript_returns_single_chunk():
    """min_messages_for_split 미만이면 LLM 호출 안 하고 단일 chunk."""
    from server.chunker import split_by_topic
    text = "[user] hi\n\n[assistant] hello\n\n[user] bye"
    fake_called = {"n": 0}
    def fake_llm(*a, **kw):
        fake_called["n"] += 1
        return "{}"
    out = split_by_topic(text, llm_summarize=fake_llm, min_messages_for_split=10)
    assert len(out) == 1
    assert out[0]["label"] == "전체"
    assert fake_called["n"] == 0  # LLM 호출 안 함


def test_split_uses_llm_response():
    from server.chunker import split_by_topic
    msgs = [f"[user] message {i}" for i in range(20)]
    text = "\n\n".join(msgs)
    fake_resp = json.dumps({
        "chunks": [
            {"start": 0, "label": "초반"},
            {"start": 10, "label": "후반"},
        ]
    })
    out = split_by_topic(text, llm_summarize=lambda *a, **kw: fake_resp,
                         min_messages_for_split=10, min_messages_per_chunk=2)
    assert len(out) == 2
    assert out[0]["label"] == "초반"
    assert out[1]["label"] == "후반"
    # 첫 chunk는 0~9, 두번째는 10~19
    assert out[0]["text"].startswith("[user] message 0")
    assert out[1]["text"].startswith("[user] message 10")


def test_split_llm_failure_falls_back_to_single():
    from server.chunker import split_by_topic
    msgs = [f"[user] m{i}" for i in range(15)]
    text = "\n\n".join(msgs)
    def raising(*a, **kw):
        raise RuntimeError("api fail")
    out = split_by_topic(text, llm_summarize=raising, min_messages_for_split=10)
    assert len(out) == 1
    assert out[0]["label"] == "전체"


def test_split_invalid_json_falls_back_to_single():
    from server.chunker import split_by_topic
    msgs = [f"[user] m{i}" for i in range(15)]
    text = "\n\n".join(msgs)
    out = split_by_topic(text, llm_summarize=lambda *a, **kw: "not valid json",
                         min_messages_for_split=10)
    assert len(out) == 1


def test_split_merges_too_small_chunks():
    """min_messages_per_chunk 미달인 chunk는 옆 chunk와 합침."""
    from server.chunker import split_by_topic
    msgs = [f"[user] m{i}" for i in range(20)]
    text = "\n\n".join(msgs)
    # boundary 0, 2, 18 — 가운데 chunk가 16개로 크지만 마지막 chunk 2개로 너무 작음
    fake_resp = json.dumps({"chunks": [
        {"start": 0, "label": "a"}, {"start": 2, "label": "b"}, {"start": 18, "label": "c"},
    ]})
    out = split_by_topic(
        text, llm_summarize=lambda *a, **kw: fake_resp,
        min_messages_for_split=10, min_messages_per_chunk=4,
    )
    # 2번째 chunk(2) sz=16 OK, 3번째 chunk(18) sz=2 < 4 → 흡수됨
    # 1번째 chunk(0) sz=2 < 4 → 흡수됨 (다음 chunk가 큼) — 그런데 첫 chunk는 흡수 못함
    # _enforce_min_size는 small chunk를 이전 chunk에 흡수. 첫 chunk가 작으면 그대로 두고
    # 두번째는 정상이라 keep, 세번째는 흡수되어 사라짐 → 2개 남음.
    assert len(out) == 2
    assert out[-1]["end"] == 20  # 마지막 chunk가 끝까지 확장