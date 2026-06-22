"""Tests verifying ExecutePhase uses its focused sub-protocol (ExecutePhaseOps)."""

from __future__ import annotations

from typing import Any

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from devloop.phases.execute import ExecutePhase
from devloop.phases.phase_ops import PhaseOps
from devloop.shared import JobStatus


class TestExecutePhaseUsesExecuteOpsSubProtocol:
    """ExecutePhase must use its focused ExecutePhaseOps sub-protocol."""

    @pytest.mark.asyncio
    async def test_uses_execute_ops_for_kpi_bump(self) -> None:
        """ExecutePhase accesses kpi_bump via execute_ops sub-protocol."""
        phase = ExecutePhase()

        async def _kpi(name: str, val: int) -> None:
            _kpi._called = (name, val)

        _kpi._called = None  # type: ignore[attr-defined]

        callbacks = PhaseOps(
            dispatch_execute=AsyncMock(
                return_value=MagicMock(
                    status=JobStatus.COMPLETE.value,
                    commits=2,
                    branch="feat/1",
                    pr_url="https://github.com/p/r/1",
                )
            ),
            comment=AsyncMock(),
            kpi_bump=AsyncMock(),
        )
        # Also set execute_ops.kpi_bump so the phase can call it.
        callbacks.execute_ops.kpi_bump = _kpi  # type: ignore[assignment]

        inp = MagicMock(
            project_id="proj",
            execute_max_iterations=1,
            poll_interval_seconds=5.0,
            ci_fix_max_iterations=3,
        )

        with patch("devloop.phases.execute.workflow") as mock_workflow:
            mock_workflow.execute_activity = AsyncMock()
            _ = await phase.run(inp=inp, issue={"id": "42"}, callbacks=callbacks)

        # execute_ops.kpi_bump must have been called — this verifies the
        # phase accesses kpi_bump through the execute_ops sub-protocol.
        assert _kpi._called == ("execute_attempts", 1)

    @pytest.mark.asyncio
    async def test_uses_execute_ops_for_comment(self) -> None:
        """ExecutePhase accesses comment via execute_ops sub-protocol."""
        phase = ExecutePhase()

        callbacks = PhaseOps(
            dispatch_execute=AsyncMock(
                return_value=MagicMock(
                    status=JobStatus.COMPLETE.value,
                    commits=2,
                    branch="feat/1",
                    pr_url="https://github.com/p/r/1",
                )
            ),
        )
        # Set execute_ops.comment so the phase can call it.
        callbacks.execute_ops.comment = AsyncMock()

        inp = MagicMock(
            project_id="proj",
            execute_max_iterations=1,
            poll_interval_seconds=5.0,
            ci_fix_max_iterations=3,
        )

        with patch("devloop.phases.execute.workflow") as mock_workflow:
            mock_workflow.execute_activity = AsyncMock()
            _ = await phase.run(inp=inp, issue={"id": "42"}, callbacks=callbacks)

        # execute_ops.comment must have been called — this verifies the
        # phase accesses comment through the execute_ops sub-protocol.
        assert callbacks.execute_ops.comment is not None
        callbacks.execute_ops.comment.assert_awaited()

    @pytest.mark.asyncio
    async def test_uses_execute_ops_for_dispatch_execute(self) -> None:
        """ExecutePhase accesses dispatch_execute via execute_ops sub-protocol."""
        phase = ExecutePhase()

        async def _dispatch(
            project_id: str, spec: Any, issue_number: int, poll: float
        ) -> Any:
            _dispatch._called = (project_id, issue_number)
            return MagicMock(
                status=JobStatus.COMPLETE.value,
                commits=2,
                branch="feat/1",
                pr_url="https://github.com/p/r/1",
            )

        _dispatch._called = False  # type: ignore[attr-defined]

        callbacks = PhaseOps(
            comment=AsyncMock(),
        )
        callbacks.execute_ops.dispatch_execute = _dispatch  # type: ignore[assignment]

        inp = MagicMock(
            project_id="proj",
            execute_max_iterations=1,
            poll_interval_seconds=5.0,
            ci_fix_max_iterations=3,
        )

        with patch("devloop.phases.execute.workflow") as mock_workflow:
            mock_workflow.execute_activity = AsyncMock()
            _ = await phase.run(inp=inp, issue={"id": "42"}, callbacks=callbacks)

        # execute_ops.dispatch_execute must have been called.
        assert callbacks.execute_ops.dispatch_execute is not None
        assert _dispatch._called == ("proj", 42)  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_execute_ops_fallback_uses_phaseops_field(self) -> None:
        """When execute_ops.comment is None, ExecutePhase falls back to PhaseOps.comment."""
        phase = ExecutePhase()

        callbacks = PhaseOps(
            dispatch_execute=AsyncMock(
                return_value=MagicMock(
                    status=JobStatus.COMPLETE.value,
                    commits=2,
                    branch="feat/1",
                    pr_url="https://github.com/p/r/1",
                )
            ),
            comment=AsyncMock(),
            kpi_bump=AsyncMock(),
        )
        # execute_ops.comment is None (default), so it should fall back.
        callbacks.execute_ops.comment = None

        inp = MagicMock(
            project_id="proj",
            execute_max_iterations=1,
            poll_interval_seconds=5.0,
            ci_fix_max_iterations=3,
        )

        with patch("devloop.phases.execute.workflow") as mock_workflow:
            mock_workflow.execute_activity = AsyncMock()
            _ = await phase.run(inp=inp, issue={"id": "42"}, callbacks=callbacks)

        # PhaseOps.comment should have been called as fallback.
        callbacks.comment.assert_awaited()
