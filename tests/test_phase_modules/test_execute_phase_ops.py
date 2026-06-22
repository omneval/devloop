"""Tests for ExecutePhaseOps — the focused execute-phase callback protocol."""

from __future__ import annotations

import inspect
from typing import Any


from devloop.phases.phase_ops import PhaseOps


class TestExecutePhaseOpsFields:
    """ExecutePhaseOps exposes exactly 4 fields."""

    def test_has_comment_field(self) -> None:
        """ExecutePhaseOps has a comment field."""
        from devloop.phases.execute_phase_ops import ExecutePhaseOps

        ops = ExecutePhaseOps()
        assert hasattr(ops, "comment")
        assert ops.comment is None

    def test_has_dispatch_execute_field(self) -> None:
        """ExecutePhaseOps has a dispatch_execute field."""
        from devloop.phases.execute_phase_ops import ExecutePhaseOps

        ops = ExecutePhaseOps()
        assert hasattr(ops, "dispatch_execute")
        assert ops.dispatch_execute is None

    def test_has_answer_question_field(self) -> None:
        """ExecutePhaseOps has an answer_question field."""
        from devloop.phases.execute_phase_ops import ExecutePhaseOps

        ops = ExecutePhaseOps()
        assert hasattr(ops, "answer_question")
        assert ops.answer_question is None

    def test_has_kpi_bump_field(self) -> None:
        """ExecutePhaseOps has a kpi_bump field."""
        from devloop.phases.execute_phase_ops import ExecutePhaseOps

        ops = ExecutePhaseOps()
        assert hasattr(ops, "kpi_bump")
        assert ops.kpi_bump is None

    def test_has_exactly_four_fields(self) -> None:
        """ExecutePhaseOps has exactly 4 data fields."""
        from devloop.phases.execute_phase_ops import ExecutePhaseOps

        # The data fields (not counting __module__, __doc__, __dict__, etc.)
        sig = inspect.signature(ExecutePhaseOps.__init__)
        params = {p for p in sig.parameters if p not in ("self", "kwargs")}
        assert params == {"comment", "dispatch_execute", "answer_question", "kpi_bump"}

    def test_can_set_all_fields(self) -> None:
        """ExecutePhaseOps can be instantiated with all fields."""
        from devloop.phases.execute_phase_ops import ExecutePhaseOps

        async def noop(*a: Any, **kw: Any) -> None:
            pass

        ops = ExecutePhaseOps(
            comment=noop,
            dispatch_execute=noop,
            answer_question=noop,
            kpi_bump=noop,
        )
        assert ops.comment is noop
        assert ops.dispatch_execute is noop
        assert ops.answer_question is noop
        assert ops.kpi_bump is noop


class TestExecutePhaseOpsDefault:
    """ExecutePhaseOps.default() returns an instance with all None."""

    def test_default_returns_instance(self) -> None:
        """ExecutePhaseOps.default() returns an ExecutePhaseOps instance."""
        from devloop.phases.execute_phase_ops import ExecutePhaseOps

        ops = ExecutePhaseOps.default()
        assert isinstance(ops, ExecutePhaseOps)

    def test_default_all_none(self) -> None:
        """ExecutePhaseOps.default() sets all fields to None."""
        from devloop.phases.execute_phase_ops import ExecutePhaseOps

        ops = ExecutePhaseOps.default()
        assert ops.comment is None
        assert ops.dispatch_execute is None
        assert ops.answer_question is None
        assert ops.kpi_bump is None


class TestExecutePhaseOpsBackwardCompat:
    """PhaseOps exposes execute_ops sub-protocol for backward compatibility."""

    def test_phaseops_has_execute_ops(self) -> None:
        """PhaseOps exposes an execute_ops property returning the sub-protocol."""
        from devloop.phases.execute_phase_ops import ExecutePhaseOps

        ops = PhaseOps()
        assert hasattr(ops, "execute_ops")
        assert isinstance(ops.execute_ops, ExecutePhaseOps)

    def test_phaseops_execute_ops_has_expected_fields(self) -> None:
        """PhaseOps.execute_ops has the same fields as ExecutePhaseOps."""

        phase_ops = PhaseOps()
        for field in ("comment", "dispatch_execute", "answer_question", "kpi_bump"):
            assert hasattr(phase_ops.execute_ops, field)


class TestExecutePhaseOpsInPhaseOps:
    """PhaseOps.__init__ packages execute fields into execute_ops."""

    def test_phaseops_packages_execute_fields(self) -> None:
        """PhaseOps constructor packages execute-specific fields into execute_ops."""
        from devloop.phases.execute_phase_ops import ExecutePhaseOps

        async def noop(*a: Any, **kw: Any) -> None:
            pass

        ops = PhaseOps(
            comment=noop,
            dispatch_execute=noop,
            answer_question=noop,
            kpi_bump=noop,
        )
        assert isinstance(ops.execute_ops, ExecutePhaseOps)
        assert ops.execute_ops.comment is noop
        assert ops.execute_ops.dispatch_execute is noop
        assert ops.execute_ops.answer_question is noop
        assert ops.execute_ops.kpi_bump is noop
