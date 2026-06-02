"""Tests for the Slack text clamps."""

from text_utils import MAX_MESSAGE, MAX_THREAD_TOPIC, TRUNC_MARKER, clamp


def test_clamp_short_text_unchanged():
    assert clamp("hello", MAX_MESSAGE, TRUNC_MARKER) == "hello"


def test_clamp_none_is_empty():
    assert clamp(None, MAX_MESSAGE) == ""


def test_clamp_message_to_limit_with_marker():
    text = "x" * 5000
    out = clamp(text, MAX_MESSAGE, TRUNC_MARKER)
    assert len(out) == MAX_MESSAGE
    assert out.endswith(TRUNC_MARKER)


def test_clamp_thread_topic_no_marker():
    out = clamp("a" * 250, MAX_THREAD_TOPIC)
    assert len(out) == MAX_THREAD_TOPIC
    assert out == "a" * MAX_THREAD_TOPIC


def test_clamp_marker_longer_than_limit_falls_back_to_hard_cut():
    out = clamp("abcdefghij", 3, marker="…………")
    assert out == "abc"
