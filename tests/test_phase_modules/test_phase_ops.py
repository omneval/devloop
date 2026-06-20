"""Tests for devloop.phases.phase_ops — unified PhaseOps callback protocol."""

from __future__ import annotations

import inspect



# All known PhaseOps attribute names (data attributes + properties + classmethods).
_KNOWN_ATTRS = frozenset(
    {
        # Core operations
        "comment",
        "cleanup",
        "dispatch",
        "kpi_bump",
        "poll_ci",
        "request_reviewer",
        # ExecutePhase
        "dispatch_execute",
        "answer_question",
        # ReviewPhase
        "dispatch_review",
        "post_review_findings",
        # PlanPhase
        "plan_issue",
        "dispatch_plan",
        "drop_issues_in_review",
        # KPI emission
        "kpi_take",
        "emit_kpis",
        # Backward-compat aliases
        "post_comment",
        "phaseops",
    }
)


class TestPhaseOpsProtocol:
    """PhaseOps — the unified I/O adapter protocol for all phase modules."""

    def test_importable_from_phases_module(self) -> None:
        """PhaseOps can be imported from devloop.phases.phase_ops."""
        from devloop.phases.phase_ops import PhaseOps

        assert PhaseOps is not None

    def test_has_required_operations(self) -> None:
        """PhaseOps covers the required operations: comment, cleanup, dispatch,
        kpi_bump, poll_ci, request_reviewer."""
        from devloop.phases.phase_ops import PhaseOps

        # Check constructor parameter names
        sig = inspect.signature(PhaseOps.__init__)
        params = {p for p in sig.parameters if p != "self"}
        required = {
            "comment",
            "cleanup",
            "dispatch",
            "kpi_bump",
            "poll_ci",
            "request_reviewer",
        }
        for req in required:
            assert req in params, (
                f"PhaseOps.__init__ is missing required operation: {req}"
            )

    def test_has_phase_specific_operations(self) -> None:
        """PhaseOps covers phase-specific operations so that any phase can use
        the same protocol without needing its own dataclass."""
        from devloop.phases.phase_ops import PhaseOps

        sig = inspect.signature(PhaseOps.__init__)
        params = {p for p in sig.parameters if p != "self"}
        phase_specific = {
            # ExecutePhase
            "dispatch_execute",
            "answer_question",
            # ReviewPhase
            "dispatch_review",
            "post_review_findings",
            # CICycle (maps to dispatch)
            # ReviewFixPass (maps to dispatch)
            # PlanPhase
            "plan_issue",
            "dispatch_plan",
            "drop_issues_in_review",
            # KPI emission
            "kpi_take",
            "emit_kpis",
            # Notifier
        }
        for ps in phase_specific:
            assert ps in params, (
                f"PhaseOps.__init__ is missing phase-specific operation: {ps}"
            )

    def test_has_default_classmethod(self) -> None:
        """PhaseOps has a default() classmethod that returns an instance."""
        from devloop.phases.phase_ops import PhaseOps

        instance = PhaseOps.default()
        assert isinstance(instance, PhaseOps)

    def test_default_has_nothing_set(self) -> None:
        """PhaseOps.default() returns an instance with all fields None."""
        from devloop.phases.phase_ops import PhaseOps

        instance = PhaseOps.default()
        sig = inspect.signature(PhaseOps.__init__)
        params = {p for p in sig.parameters if p != "self"}
        for attr in params:
            assert getattr(instance, attr) is None, (
                f"Expected {attr} to be None in default()"
            )

    def test_can_set_individual_fields(self) -> None:
        """PhaseOps can be instantiated with individual fields set."""
        from devloop.phases.phase_ops import PhaseOps

        callback = lambda *a, **kw: None  # noqa: E731
        instance = PhaseOps(comment=callback)
        assert instance.comment is callback
        assert instance.cleanup is None

    def test_default_is_different_instance(self) -> None:
        """Calling default() twice returns different instances."""
        from devloop.phases.phase_ops import PhaseOps

        a = PhaseOps.default()
        b = PhaseOps.default()
        assert a is not b


class TestPhaseOpsReExport:
    """PhaseOps should be re-exported from devloop.phases for convenience."""

    def test_importable_from_phases_package(self) -> None:
        """PhaseOps can be imported from devloop.phases."""
        from devloop.phases import PhaseOps

        assert PhaseOps is not None
