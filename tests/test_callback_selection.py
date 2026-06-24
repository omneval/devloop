"""Tests for the callback-selection utility in devloop.phases._utils."""

from devloop.phases._utils import callback_or_ops


def test_selects_ops_field_when_set():
    """When ops_field is set, it should be selected."""

    def mock_callback():
        pass

    assert callback_or_ops(mock_callback, None) is mock_callback


def test_selects_cb_field_when_ops_is_none():
    """When ops_field is None, cb.field should be selected."""
    value = lambda: None  # noqa: E731

    assert callback_or_ops(None, value) is value


def test_selects_default_cb_field_when_ops_and_cb_are_none():
    """When both ops_field and cb.field are None, cb.default should be selected."""
    ops_value = None
    cb_value = None
    default_value = lambda: None  # noqa: E731

    assert callback_or_ops(ops_value, cb_value, default_value) is default_value


def test_favors_ops_over_cb():
    """ops takes priority over cb, regardless of cb value."""
    ops_value = lambda: "ops"  # noqa: E731
    cb_value = lambda: "cb"  # noqa: E731

    result = callback_or_ops(ops_value, cb_value, cb_value)
    assert result is ops_value


def test_favors_cb_over_default():
    """cb takes priority over default when ops is None."""
    ops_value = None
    cb_value = lambda: "cb"  # noqa: E731
    default_value = lambda: "default"  # noqa: E731

    result = callback_or_ops(ops_value, cb_value, default_value)
    assert result is cb_value


def test_all_none_returns_none():
    """When all sources are None, result is None."""
    assert callback_or_ops(None, None, None) is None


def test_empty_sequence_returns_last_value():
    """Two-source variant returns ops or cb."""
    ops_value = None
    cb_value = lambda: "cb"  # noqa: E731

    result = callback_or_ops(ops_value, cb_value)
    assert result is cb_value
