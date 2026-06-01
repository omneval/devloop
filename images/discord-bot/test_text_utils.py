"""Tests for the Discord text clamps (prevent 400 Invalid Form Body)."""

from text_utils import MAX_MESSAGE, MAX_THREAD_NAME, TRUNC_MARKER, clamp


def test_clamp_short_text_unchanged():
    assert clamp("hello", MAX_MESSAGE, TRUNC_MARKER) == "hello"


def test_clamp_none_is_empty():
    assert clamp(None, MAX_MESSAGE) == ""


def test_clamp_message_to_limit_with_marker():
    text = "x" * 5000  # an over-long diagnosis root-cause blob
    out = clamp(text, MAX_MESSAGE, TRUNC_MARKER)
    assert len(out) == MAX_MESSAGE
    assert out.endswith(TRUNC_MARKER)


def test_clamp_thread_name_no_marker():
    out = clamp("a" * 250, MAX_THREAD_NAME)
    assert len(out) == MAX_THREAD_NAME
    assert out == "a" * MAX_THREAD_NAME


def test_clamp_marker_longer_than_limit_falls_back_to_hard_cut():
    out = clamp("abcdefghij", 3, marker="…………")  # marker longer than limit
    assert out == "abc"
