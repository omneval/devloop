"""Tests for consolidated `_as_int` utility.

These tests verify that the `_as_int` helper lives in a single shared location
(`devloop._constants`) and that every module that previously defined its own local
copy now imports from there.  This ensures locality: the code quality improvement
that eliminates the 7 duplicate `_as_int` definitions across the codebase.
"""

from __future__ import annotations


class TestAsIntFromShared:
    """The `_as_int` function is importable from ``devloop._constants``."""

    def test_imports_as_int(self) -> None:
        from devloop._constants import _as_int

        assert callable(_as_int)

    def test_as_int_valid_int(self) -> None:
        from devloop._constants import _as_int

        assert _as_int(42) == 42

    def test_as_int_string_int(self) -> None:
        from devloop._constants import _as_int

        assert _as_int("123") == 123

    def test_as_int_float(self) -> None:
        from devloop._constants import _as_int

        assert _as_int(3.7) == 3

    def test_as_int_empty_string(self) -> None:
        from devloop._constants import _as_int

        assert _as_int("") == 0

    def test_as_int_none(self) -> None:
        from devloop._constants import _as_int

        assert _as_int(None) == 0

    def test_as_int_invalid_string(self) -> None:
        from devloop._constants import _as_int

        assert _as_int("not_a_number") == 0

    def test_as_int_negative(self) -> None:
        from devloop._constants import _as_int

        assert _as_int("-7") == -7

    def test_as_int_zero(self) -> None:
        from devloop._constants import _as_int

        assert _as_int(0) == 0


class TestPhaseOpsAsIntUsesShared:
    """``PhaseOps.as_int`` delegates to the shared ``_constants._as_int``."""

    def test_phaseops_as_int(self) -> None:
        from devloop.phases.phase_ops import PhaseOps

        ops = PhaseOps()
        assert ops.as_int(42) == 42
        assert ops.as_int("") == 0
        assert ops.as_int("abc") == 0
