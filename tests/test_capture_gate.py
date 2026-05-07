"""capture 게이트 (should_capture / filter_capture_items) 단위 테스트."""
from __future__ import annotations


def _good() -> dict:
    """게이트 통과 기본 항목."""
    return {
        "type": "fact",
        "description": "결제팀 PM은 김OO이며 정산 도메인 담당",
        "content": "결제팀 PM은 김OO이며 정산 도메인을 담당. 2026 Q2 합류.",
        "confidence": 0.85,
        "tags": ["team:billing", "source:declared"],
    }


# ── should_capture ────────────────────────────────────────

def test_good_item_accepted():
    from server.memory_store import should_capture
    ok, reason = should_capture(_good())
    assert ok is True and reason == "ok"


def test_invalid_type_rejected():
    from server.memory_store import should_capture
    item = _good() | {"type": "note"}
    ok, reason = should_capture(item)
    assert ok is False
    assert reason.startswith("invalid_type")


def test_short_description_rejected():
    from server.memory_store import should_capture
    item = _good() | {"description": "짧음"}
    ok, reason = should_capture(item)
    assert ok is False
    assert reason.startswith("desc_too_short")


def test_short_content_rejected():
    from server.memory_store import should_capture
    item = _good() | {"content": "한줄"}
    ok, reason = should_capture(item)
    assert ok is False
    assert reason.startswith("content_too_short")


def test_low_confidence_rejected():
    from server.memory_store import should_capture
    item = _good() | {"confidence": 0.55}
    ok, reason = should_capture(item)
    assert ok is False
    assert reason.startswith("low_confidence")


def test_confidence_at_floor_accepted():
    """0.60 정확히는 accept (>=)."""
    from server.memory_store import should_capture, CAPTURE_MIN_CONFIDENCE
    item = _good() | {"confidence": CAPTURE_MIN_CONFIDENCE}
    ok, _ = should_capture(item)
    assert ok is True


def test_invalid_confidence_string_rejected():
    from server.memory_store import should_capture
    item = _good() | {"confidence": "high"}
    ok, reason = should_capture(item)
    assert ok is False
    assert reason == "invalid_confidence"


def test_pure_numeric_description_rejected():
    """description이 숫자/기호만이면 의미 토큰 0개로 판정."""
    from server.memory_store import should_capture
    item = _good() | {"description": "12345 67890 !!!! ----"}
    ok, reason = should_capture(item)
    assert ok is False
    assert reason == "no_meaningful_tokens"


# ── filter_capture_items ──────────────────────────────────

def test_filter_separates_kept_and_rejected_with_reasons():
    from server.memory_store import filter_capture_items

    items = [
        _good(),                               # ok
        _good() | {"description": "짧"},        # desc_too_short
        _good() | {"confidence": 0.4},          # low_confidence
        _good() | {"type": "note"},             # invalid_type
        _good() | {"description": "두번째 정상 메모리 항목 description"},  # ok
    ]
    kept, reasons = filter_capture_items(items)
    assert len(kept) == 2
    assert sum(reasons.values()) == 3
    assert any(k.startswith("desc_too_short") for k in reasons)
    assert any(k.startswith("low_confidence") for k in reasons)
    assert any(k.startswith("invalid_type") for k in reasons)


def test_filter_handles_empty_input():
    from server.memory_store import filter_capture_items
    kept, reasons = filter_capture_items([])
    assert kept == []
    assert reasons == {}
