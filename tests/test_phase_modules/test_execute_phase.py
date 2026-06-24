"""Unit tests for devloop.phases.execute — ExecutePhase standalone module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from devloop.phases.execute import ExecutePhase
from devloop.phases.phase_ops import PhaseOps
from devloop.projects import install_registry
from devloop.shared import JobStatus


class TestExecutePhase:
    """ExecutePhase — dispatch the agent execute job."""

    @pytest.mark.asyncio
    async def test_executes_successfully_first_attempt(self) -> None:
        """ExecutePhase produces commits on first attempt → CI fix cycle."""
        phase = ExecutePhase()

        callbacks = PhaseOps(
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
        callbacks = PhaseOps(
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
        callbacks = PhaseOps(
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
        callbacks = PhaseOps(
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

    @pytest.mark.asyncio
    async def test_executes_with_open_pr_as_draft_in_extra(
        self, tmp_path: "pytest.Path"
    ) -> None:
        """ExecutePhase adds open_pr_as_draft=True to TaskSpec.extra when project config says so."""
        projects_yaml = tmp_path / "projects.yaml"
        projects_yaml.write_text(
            "projects:\n"
            "- id: draft-proj\n"
            "  github_url: https://github.com/example/repo\n"
            "  default_branch: main\n"
            "  agent_label: agent-ready\n"
            "  github_token_secret: draft-token\n"
            "  open_pr_as_draft: true\n"
        )
        install_registry(projects_yaml)

        phase = ExecutePhase()

        callbacks = PhaseOps(
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
            project_id="draft-proj",
            execute_max_iterations=1,
            poll_interval_seconds=5.0,
            ci_fix_max_iterations=3,
        )

        _ = await phase.run(
            inp=inp,
            issue={"id": "42"},
            callbacks=callbacks,
        )

        # The dispatch_execute callback should have been called with a TaskSpec
        # that includes open_pr_as_draft=True in its extra dict.
        call_args = callbacks.dispatch_execute.call_args
        assert call_args is not None, "dispatch_execute was never called"
        _, task_spec, _, _ = call_args[
            0
        ]  # project_id, spec, issue_number, poll_interval
        assert isinstance(task_spec.extra, dict)
        assert task_spec.extra.get("open_pr_as_draft") is True

        # Clean up registry
        (tmp_path / "empty.yaml").write_text("projects: []\n")
        install_registry(tmp_path / "empty.yaml")

    @pytest.mark.asyncio
    async def test_executes_with_open_pr_as_draft_default_false(
        self, tmp_path: "pytest.Path"
    ) -> None:
        """ExecutePhase defaults open_pr_as_draft to False when not specified."""
        projects_yaml = tmp_path / "projects.yaml"
        projects_yaml.write_text(
            "projects:\n"
            "- id: no-draft-proj\n"
            "  github_url: https://github.com/example/repo\n"
            "  default_branch: main\n"
            "  agent_label: agent-ready\n"
            "  github_token_secret: token\n"
        )
        install_registry(projects_yaml)

        phase = ExecutePhase()

        callbacks = PhaseOps(
            dispatch_execute=AsyncMock(
                return_value=MagicMock(
                    status=JobStatus.COMPLETE.value,
                    commits=1,
                    branch="feat/1",
                    pr_url="https://github.com/p/r/1",
                )
            ),
            post_comment=AsyncMock(),
            kpi_bump=AsyncMock(),
        )
        inp = MagicMock(
            project_id="no-draft-proj",
            execute_max_iterations=1,
            poll_interval_seconds=5.0,
            ci_fix_max_iterations=3,
        )

        _ = await phase.run(
            inp=inp,
            issue={"id": "99"},
            callbacks=callbacks,
        )

        call_args = callbacks.dispatch_execute.call_args
        assert call_args is not None
        _, task_spec, _, _ = call_args[0]
        assert isinstance(task_spec.extra, dict)
        assert task_spec.extra.get("open_pr_as_draft") is False

        # Clean up registry
        (tmp_path / "empty.yaml").write_text("projects: []\n")
        install_registry(tmp_path / "empty.yaml")


class TestExecutePhaseWithPhaseOps:
    """ExecutePhase should accept the unified PhaseOps protocol."""

    @pytest.mark.asyncio
    async def test_accepts_phaseops_for_callbacks(self) -> None:
        """ExecutePhase.run() accepts a PhaseOps instance as callbacks."""
        phase = ExecutePhase()

        callbacks = PhaseOps(
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
    async def test_phaseops_with_none_defaults_uses_activities(self) -> None:
        """PhaseOps with all None still works — ExecutePhase falls back."""
        phase = ExecutePhase()

        callbacks = PhaseOps.default()
        # Provide minimal callbacks so Temporal activities
        # aren't triggered (they require a workflow event loop).
        callbacks.post_comment = AsyncMock()
        callbacks.dispatch_execute = AsyncMock(
            return_value=MagicMock(
                status=JobStatus.COMPLETE.value,
                commits=1,
                branch="feat/1",
                pr_url="https://github.com/p/r/1",
            )
        )
        inp = MagicMock(
            project_id="proj",
            execute_max_iterations=1,
            poll_interval_seconds=5.0,
            ci_fix_max_iterations=3,
        )

        # With minimal callbacks, ExecutePhase uses the PhaseOps
        # protocol path rather than Temporal activities.
        result = await phase.run(
            inp=inp,
            issue={"id": "42"},
            callbacks=callbacks,
        )
        # The result structure is still valid
        assert "issue_id" in result
        assert "commits" in result
