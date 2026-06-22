"""Tests for CICycleOps — the focused CI fix cycle callback protocol."""

from __future__ import annotations

import inspect
from typing import Any


from devloop.phases.phase_ops import PhaseOps


class TestCICycleOpsFields:
    """CICycleOps exposes exactly 5 fields."""

    def test_has_comment_field(self) -> None:
        """CICycleOps has a comment field."""
        from devloop.phases.ci_cycle_ops import CICycleOps

        ops = CICycleOps()
        assert hasattr(ops, "comment")
        assert ops.comment is None

    def test_has_dispatch_fix_field(self) -> None:
        """CICycleOps has a dispatch_fix field."""
        from devloop.phases.ci_cycle_ops import CICycleOps

        ops = CICycleOps()
        assert hasattr(ops, "dispatch_fix")
        assert ops.dispatch_fix is None

    def test_has_poll_ci_field(self) -> None:
        """CICycleOps has a poll_ci field."""
        from devloop.phases.ci_cycle_ops import CICycleOps

        ops = CICycleOps()
        assert hasattr(ops, "poll_ci")
        assert ops.poll_ci is None

    def test_has_kpi_bump_field(self) -> None:
        """CICycleOps has a kpi_bump field."""
        from devloop.phases.ci_cycle_ops import CICycleOps

        ops = CICycleOps()
        assert hasattr(ops, "kpi_bump")
        assert ops.kpi_bump is None

    def test_has_cleanup_field(self) -> None:
        """CICycleOps has a cleanup field."""
        from devloop.phases.ci_cycle_ops import CICycleOps

        ops = CICycleOps()
        assert hasattr(ops, "cleanup")
        assert ops.cleanup is None

    def test_has_exactly_five_fields(self) -> None:
        """CICycleOps has exactly 5 data fields."""
        from devloop.phases.ci_cycle_ops import CICycleOps

        sig = inspect.signature(CICycleOps.__init__)
        params = {p for p in sig.parameters if p not in ("self", "kwargs")}
        assert params == {"comment", "dispatch_fix", "poll_ci", "kpi_bump", "cleanup"}

    def test_can_set_all_fields(self) -> None:
        """CICycleOps can be instantiated with all fields."""
        from devloop.phases.ci_cycle_ops import CICycleOps

        async def noop(*a: Any, **kw: Any) -> None:
            pass

        ops = CICycleOps(
            comment=noop,
            dispatch_fix=noop,
            poll_ci=noop,
            kpi_bump=noop,
            cleanup=noop,
        )
        assert ops.comment is noop
        assert ops.dispatch_fix is noop
        assert ops.poll_ci is noop
        assert ops.kpi_bump is noop
        assert ops.cleanup is noop


class TestCICycleOpsDefault:
    """CICycleOps.default() returns an instance with all None."""

    def test_default_returns_instance(self) -> None:
        """CICycleOps.default() returns a CICycleOps instance."""
        from devloop.phases.ci_cycle_ops import CICycleOps

        ops = CICycleOps.default()
        assert isinstance(ops, CICycleOps)

    def test_default_all_none(self) -> None:
        """CICycleOps.default() sets all fields to None."""
        from devloop.phases.ci_cycle_ops import CICycleOps

        ops = CICycleOps.default()
        assert ops.comment is None
        assert ops.dispatch_fix is None
        assert ops.poll_ci is None
        assert ops.kpi_bump is None
        assert ops.cleanup is None


class TestCICycleOpsBackwardCompat:
    """PhaseOps exposes ci_ops sub-protocol for backward compatibility."""

    def test_phaseops_has_ci_ops(self) -> None:
        """PhaseOps exposes a ci_ops property returning the sub-protocol."""
        from devloop.phases.ci_cycle_ops import CICycleOps

        ops = PhaseOps()
        assert hasattr(ops, "ci_ops")
        assert isinstance(ops.ci_ops, CICycleOps)

    def test_phaseops_ci_ops_has_expected_fields(self) -> None:
        """PhaseOps.ci_ops has the same fields as CICycleOps."""

        phase_ops = PhaseOps()
        for field in ("comment", "dispatch_fix", "poll_ci", "kpi_bump", "cleanup"):
            assert hasattr(phase_ops.ci_ops, field)


class TestCICycleOpsInPhaseOps:
    """PhaseOps.__init__ packages CI cycle fields into ci_ops."""

    def test_phaseops_packages_ci_fields(self) -> None:
        """PhaseOps constructor packages CI cycle-specific fields into ci_ops."""
        from devloop.phases.ci_cycle_ops import CICycleOps

        async def noop(*a: Any, **kw: Any) -> None:
            pass

        ops = PhaseOps(
            comment=noop,
            dispatch_fix=noop,
            poll_ci=noop,
            kpi_bump=noop,
            cleanup=noop,
        )
        assert isinstance(ops.ci_ops, CICycleOps)
        assert ops.ci_ops.comment is noop
        assert ops.ci_ops.dispatch_fix is noop
        assert ops.ci_ops.poll_ci is noop
        assert ops.ci_ops.kpi_bump is noop
        assert ops.ci_ops.cleanup is noop
