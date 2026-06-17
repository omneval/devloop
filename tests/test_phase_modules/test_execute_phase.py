"""Unit tests for devloop.phases.execute — ExecutePhase standalone module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from devloop.phases.execute import ExecutePhase, ExecutePhaseCallbacks
from devloop.shared import JobStatus


class TestExecutePhase:
    """ExecutePhase — dispatch the agent execute job."""

    @pytest.mark.asyncio
    async def test_executes_successfully_first_attempt(self) -> None:
        """ExecutePhase produces commits on first attempt → CI fix cycle."""
        phase = ExecutePhase()

        callbacks = ExecutePhaseCallbacks(
            dispatch_execute=AsyncMock(
                return_value=MagicMock(
                    status=JobStatus.COMPLETE.value,
                    commits=3,
                    branch="feat/1",
                    pr_url="https://github.com/p/r/1",
                )
            ),
            post_comment=AsyncMock(),
            kpi_bump=AsyncMock(),
        )
        inp = MagicMock(
            project_id="proj",
            execute_max_iterations=1,
            poll_interval_seconds=5.0,
            ci_fix_max_iterations=3,
        )

        result = await phase.run(
            inp=inp,
            issue={"id": "42"},
            callbacks=callbacks,
        )

        assert result["issue_id"] == 42
        assert result["commits"] == 3
        callbacks.kpi_bump.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retry_on_zero_commits(self) -> None:
        """ExecutePhase retries when status==COMPLETE but zero commits."""
        phase = ExecutePhase()
        callbacks = ExecutePhaseCallbacks(
            dispatch_execute=AsyncMock(
                side_effect=[
                    MagicMock(
                        status=JobStatus.COMPLETE.value,
                        commits=0,
                        branch="",
                        pr_url="",
                    ),
                    MagicMock(
                        status=JobStatus.COMPLETE.value,
                        commits=2,
                        branch="feat/1",
                        pr_url="https://github.com/p/r/1",
                    ),
                ]
            ),
            post_comment=AsyncMock(),
            kpi_bump=AsyncMock(),
        )
        inp = MagicMock(
            project_id="proj",
            execute_max_iterations=2,
            poll_interval_seconds=5.0,
            ci_fix_max_iterations=3,
        )

        result = await phase.run(
            inp=inp,
            issue={"id": "42"},
            callbacks=callbacks,
        )

        assert callbacks.dispatch_execute.await_count == 2
        assert result["commits"] == 2

    @pytest.mark.asyncio
    async def test_exhausted_retries_returns_zero_commits(self) -> None:
        """ExecutePhase returns exhausted result after all retries."""
        phase = ExecutePhase()
        callbacks = ExecutePhaseCallbacks(
            dispatch_execute=AsyncMock(
                side_effect=[
                    MagicMock(
                        status=JobStatus.COMPLETE.value,
                        commits=0,
                        branch="",
                        pr_url="",
                    ),
                    MagicMock(
                        status=JobStatus.COMPLETE.value,
                        commits=0,
                        branch="",
                        pr_url="",
                    ),
                ]
            ),
            post_comment=AsyncMock(),
            kpi_bump=AsyncMock(),
        )
        inp = MagicMock(
            project_id="proj",
            execute_max_iterations=2,
            poll_interval_seconds=5.0,
            ci_fix_max_iterations=3,
        )

        result = await phase.run(
            inp=inp,
            issue={"id": "42"},
            callbacks=callbacks,
        )

        assert result["commits"] == 0
        assert result["exhausted"] is False  # execute exhausted, not CI

    @pytest.mark.asyncio
    async def test_non_complete_status_parks_issue(self) -> None:
        """ExecutePhase parks the issue when status is not COMPLETE."""
        phase = ExecutePhase()
        callbacks = ExecutePhaseCallbacks(
            dispatch_execute=AsyncMock(
                return_value=MagicMock(
                    status=JobStatus.FAILED.value,
                    commits=0,
                    branch="",
                    pr_url="",
                    error="timeout",
                )
            ),
            post_comment=AsyncMock(),
            kpi_bump=AsyncMock(),
        )
        inp = MagicMock(
            project_id="proj",
            execute_max_iterations=1,
            poll_interval_seconds=5.0,
            ci_fix_max_iterations=3,
        )

        result = await phase.run(
            inp=inp,
            issue={"id": "42"},
            callbacks=callbacks,
        )

        assert result["commits"] == 0
        assert result["pr_url"] == ""
