"""Tests for PlanPhaseOps — the focused plan-phase callback protocol."""

from __future__ import annotations

import inspect
from typing import Any


from devloop.phases.phase_ops import PhaseOps


class TestPlanPhaseOpsFields:
    """PlanPhaseOps exposes exactly 4 fields."""

    def test_has_comment_field(self) -> None:
        """PlanPhaseOps has a comment field."""
        from devloop.phases.plan_phase_ops import PlanPhaseOps

        ops = PlanPhaseOps()
        assert hasattr(ops, "comment")
        assert ops.comment is None

    def test_has_plan_issue_field(self) -> None:
        """PlanPhaseOps has a plan_issue field."""
        from devloop.phases.plan_phase_ops import PlanPhaseOps

        ops = PlanPhaseOps()
        assert hasattr(ops, "plan_issue")
        assert ops.plan_issue is None

    def test_has_dispatch_plan_field(self) -> None:
        """PlanPhaseOps has a dispatch_plan field."""
        from devloop.phases.plan_phase_ops import PlanPhaseOps

        ops = PlanPhaseOps()
        assert hasattr(ops, "dispatch_plan")
        assert ops.dispatch_plan is None

    def test_has_drop_issues_in_review_field(self) -> None:
        """PlanPhaseOps has a drop_issues_in_review field."""
        from devloop.phases.plan_phase_ops import PlanPhaseOps

        ops = PlanPhaseOps()
        assert hasattr(ops, "drop_issues_in_review")
        assert ops.drop_issues_in_review is None

    def test_has_exactly_four_fields(self) -> None:
        """PlanPhaseOps has exactly 4 data fields."""
        from devloop.phases.plan_phase_ops import PlanPhaseOps

        sig = inspect.signature(PlanPhaseOps.__init__)
        params = {p for p in sig.parameters if p not in ("self", "kwargs")}
        assert params == {
            "comment",
            "plan_issue",
            "dispatch_plan",
            "drop_issues_in_review",
        }

    def test_can_set_all_fields(self) -> None:
        """PlanPhaseOps can be instantiated with all fields."""
        from devloop.phases.plan_phase_ops import PlanPhaseOps

        async def noop(*a: Any, **kw: Any) -> None:
            pass

        ops = PlanPhaseOps(
            comment=noop,
            plan_issue=noop,
            dispatch_plan=noop,
            drop_issues_in_review=noop,
        )
        assert ops.comment is noop
        assert ops.plan_issue is noop
        assert ops.dispatch_plan is noop
        assert ops.drop_issues_in_review is noop


class TestPlanPhaseOpsDefault:
    """PlanPhaseOps.default() returns an instance with all None."""

    def test_default_returns_instance(self) -> None:
        """PlanPhaseOps.default() returns a PlanPhaseOps instance."""
        from devloop.phases.plan_phase_ops import PlanPhaseOps

        ops = PlanPhaseOps.default()
        assert isinstance(ops, PlanPhaseOps)

    def test_default_all_none(self) -> None:
        """PlanPhaseOps.default() sets all fields to None."""
        from devloop.phases.plan_phase_ops import PlanPhaseOps

        ops = PlanPhaseOps.default()
        assert ops.comment is None
        assert ops.plan_issue is None
        assert ops.dispatch_plan is None
        assert ops.drop_issues_in_review is None


class TestPlanPhaseOpsBackwardCompat:
    """PhaseOps exposes plan_ops sub-protocol for backward compatibility."""

    def test_phaseops_has_plan_ops(self) -> None:
        """PhaseOps exposes a plan_ops property returning the sub-protocol."""
        from devloop.phases.plan_phase_ops import PlanPhaseOps

        ops = PhaseOps()
        assert hasattr(ops, "plan_ops")
        assert isinstance(ops.plan_ops, PlanPhaseOps)

    def test_phaseops_plan_ops_has_expected_fields(self) -> None:
        """PhaseOps.plan_ops has the same fields as PlanPhaseOps."""

        phase_ops = PhaseOps()
        for field in (
            "comment",
            "plan_issue",
            "dispatch_plan",
            "drop_issues_in_review",
        ):
            assert hasattr(phase_ops.plan_ops, field)


class TestPlanPhaseOpsInPhaseOps:
    """PhaseOps.__init__ packages plan fields into plan_ops."""

    def test_phaseops_packages_plan_fields(self) -> None:
        """PhaseOps constructor packages plan-specific fields into plan_ops."""
        from devloop.phases.plan_phase_ops import PlanPhaseOps

        async def noop(*a: Any, **kw: Any) -> None:
            pass

        ops = PhaseOps(
            comment=noop,
            plan_issue=noop,
            dispatch_plan=noop,
            drop_issues_in_review=noop,
        )
        assert isinstance(ops.plan_ops, PlanPhaseOps)
        assert ops.plan_ops.comment is noop
        assert ops.plan_ops.plan_issue is noop
        assert ops.plan_ops.dispatch_plan is noop
        assert ops.plan_ops.drop_issues_in_review is noop
