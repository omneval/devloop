"""Verify backward-compatible Callbacks shim classes have been removed.

The shim classes (ExecutePhaseCallbacks, NotifierCallbacks, PlanPhaseCallbacks,
ReviewPhaseCallbacks, ReviewFixPassCallbacks, _Callbacks) all subclassed
PhaseOps without adding behavior. This test ensures PhaseOps works directly
as a replacement for every shim.
"""

from __future__ import annotations

from devloop.phases.phase_ops import PhaseOps


class TestPhaseOpsReplacesShims:
    """PhaseOps provides everything the shim classes did."""

    def test_phaseops_default(self) -> None:
        """PhaseOps.default() returns an empty PhaseOps."""
        cb = PhaseOps.default()
        assert isinstance(cb, PhaseOps)
        assert cb.dispatch_execute is None
        assert cb.dispatch_review is None
        assert cb.dispatch_plan is None
        assert cb.dispatch_fix is None

    def test_phaseops_phaseops_property(self) -> None:
        """PhaseOps.phaseops returns self."""
        cb = PhaseOps()
        assert cb.phaseops is cb

    def test_phaseops_as_int(self) -> None:
        """PhaseOps.as_int works like the shim's as_int."""
        cb = PhaseOps()
        assert cb.as_int(42) == 42
        assert cb.as_int("0") == 0
        assert cb.as_int(None) == 0
        assert cb.as_int("abc") == 0

    def test_phaseops_constructor_accepts_all_callback_kwargs(self) -> None:
        """PhaseOps accepts every callback kwarg the shims accepted."""
        cb = PhaseOps(
            dispatch_execute=lambda *a, **k: None,  # type: ignore[arg-type]
            dispatch_review=lambda *a, **k: None,  # type: ignore[arg-type]
            dispatch_plan=lambda *a, **k: None,  # type: ignore[arg-type]
            dispatch_fix=lambda *a, **k: None,  # type: ignore[arg-type]
            request_reviewer=lambda *a, **k: None,  # type: ignore[arg-type]
            poll_ci=lambda *a, **k: None,  # type: ignore[arg-type]
            post_review_findings=lambda *a, **k: None,  # type: ignore[arg-type]
            post_comment=lambda *a, **k: None,  # type: ignore[arg-type]
            kpi_bump=lambda *a, **k: None,  # type: ignore[arg-type]
            answer_question=lambda *a, **k: None,  # type: ignore[arg-type]
            cleanup=lambda *a, **k: None,  # type: ignore[arg-type]
        )
        # If we get here, PhaseOps accepted all kwargs — test passes
        assert isinstance(cb, PhaseOps)


class TestShimClassesRemoved:
    """The backward-compatible shim classes (PhaseOps subclasses) no longer exist."""

    def test_execute_phase_no_execute_phase_callbacks(self) -> None:
        from devloop.phases import execute

        assert not hasattr(execute, "ExecutePhaseCallbacks")

    def test_notifier_no_notifier_callbacks(self) -> None:
        from devloop.phases import notifier

        assert not hasattr(notifier, "NotifierCallbacks")

    def test_plan_no_plan_phase_callbacks(self) -> None:
        from devloop.phases import plan

        assert not hasattr(plan, "PlanPhaseCallbacks")

    def test_review_no_review_phase_callbacks(self) -> None:
        from devloop.phases import review

        assert not hasattr(review, "ReviewPhaseCallbacks")

    def test_review_fix_pass_no_review_fix_pass_callbacks(self) -> None:
        from devloop.phases import review_fix_pass

        assert not hasattr(review_fix_pass, "ReviewFixPassCallbacks")

    def test_cycle_no_phaseops_subclass(self) -> None:
        from devloop.phases import cycle

        assert not hasattr(cycle, "_Callbacks")
        assert not hasattr(cycle, "CICycleCallbacks")
