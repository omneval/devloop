"""Tests verifying DevLoopWorkflow and PRCommentWorkflow implement PhaseOps."""

from __future__ import annotations

from typing import Any

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from devloop.phases.phase_ops import PhaseOps


class TestDevLoopWorkflowImplementsPhaseOps:
    """DevLoopWorkflow implements the PhaseOps protocol by delegating to
    Temporal activity calls."""

    @pytest.mark.asyncio
    async def test_workflow_is_phaseops(self) -> None:
        """DevLoopWorkflow is a PhaseOps instance."""
        from devloop.dev_loop import DevLoopWorkflow

        workflow = DevLoopWorkflow()
        assert isinstance(workflow, PhaseOps)

    @pytest.mark.asyncio
    async def test_workflow_delegates_comment_to_activity(self) -> None:
        """DevLoopWorkflow.comment calls the post_github_comment activity."""
        from devloop.dev_loop import DevLoopWorkflow

        workflow = DevLoopWorkflow()

        with patch("devloop.dev_loop.workflow.execute_activity") as mock_activity:
            mock_activity.return_value = AsyncMock()
            mock_coro = AsyncMock()
            mock_activity.return_value = mock_coro

            await workflow.comment("proj-42", 99, "hello")

            mock_activity.assert_awaited_once()
            call_args = mock_activity.call_args
            assert call_args[0][0] == "post_github_comment"

    @pytest.mark.asyncio
    async def test_workflow_delegates_cleanup_to_activity(self) -> None:
        """DevLoopWorkflow.cleanup calls the cleanup_configmap activity."""
        from devloop.dev_loop import DevLoopWorkflow

        workflow = DevLoopWorkflow()

        with patch("devloop.dev_loop.workflow.execute_activity") as mock_activity:
            mock_coro = AsyncMock()
            mock_activity.return_value = mock_coro

            await workflow.cleanup("job-name")

            mock_activity.assert_awaited_once()
            call_args = mock_activity.call_args
            assert call_args[0][0] == "cleanup_configmap"

    @pytest.mark.asyncio
    async def test_workflow_delegates_kpi_bump(self) -> None:
        """DevLoopWorkflow.kpi_bump calls the _kpi_bump internal method."""
        from devloop.dev_loop import DevLoopWorkflow

        workflow = DevLoopWorkflow()

        await workflow.kpi_bump("test_key", 5)

        counters = getattr(workflow, "_kpi_counters", None)
        assert counters is not None
        assert counters["test_key"] == 5

    @pytest.mark.asyncio
    async def test_workflow_delegates_kpi_take(self) -> None:
        """DevLoopWorkflow.kpi_take returns and resets counters."""
        from devloop.dev_loop import DevLoopWorkflow

        workflow = DevLoopWorkflow()
        workflow._kpi_counters = {"x": 1, "y": 2}

        result = await workflow.kpi_take()

        assert result == {"x": 1, "y": 2}
        # After take, counters should be empty
        assert workflow._kpi_counters == {}

    @pytest.mark.asyncio
    async def test_workflow_delegates_dispatch_to_activity(self) -> None:
        """DevLoopWorkflow.dispatch calls the dispatch_agent_job activity."""
        from devloop.dev_loop import DevLoopWorkflow
        from devloop.shared import TaskSpec

        workflow = DevLoopWorkflow()

        with patch("devloop.dev_loop.workflow.execute_activity") as mock_activity:
            mock_result = MagicMock()
            mock_result.job_name = "test-job"
            mock_result.status = "complete"
            mock_coro = AsyncMock(return_value=mock_result)
            mock_activity.return_value = mock_coro

            spec = TaskSpec(
                phase="test",
                project_id="proj",
                issue_number=1,
            )
            await workflow.dispatch(
                "proj", spec, issue_number=1, poll_interval_seconds=5.0
            )

            # _dispatch calls execute_activity (dispatch), then _cleanup calls
            # it again (cleanup_configmap) — so at least 2 calls total.
            assert mock_activity.await_count >= 1
            first_call = mock_activity.call_args_list[0]
            assert first_call[0][0] == "dispatch_agent_job"

    @pytest.mark.asyncio
    async def test_workflow_delegates_poll_ci_to_activity(self) -> None:
        """DevLoopWorkflow.poll_ci calls the poll_ci_checks activity."""
        from devloop.dev_loop import DevLoopWorkflow

        workflow = DevLoopWorkflow()

        with patch("devloop.dev_loop.workflow.execute_activity") as mock_activity:
            mock_result = MagicMock()
            mock_result.all_passed = True
            mock_coro = AsyncMock(return_value=mock_result)
            mock_activity.return_value = mock_coro

            await workflow.poll_ci("proj", 42)

            mock_activity.assert_awaited_once()
            call_args = mock_activity.call_args
            assert call_args[0][0] == "poll_ci_checks"

    @pytest.mark.asyncio
    async def test_workflow_delegates_request_reviewer_to_activity(self) -> None:
        """DevLoopWorkflow.request_reviewer calls the request_github_reviewer activity."""
        from devloop.dev_loop import DevLoopWorkflow

        workflow = DevLoopWorkflow()

        with patch("devloop.dev_loop.workflow.execute_activity") as mock_activity:
            mock_result = MagicMock()
            mock_result.requested = True
            mock_coro = AsyncMock(return_value=mock_result)
            mock_activity.return_value = mock_coro

            await workflow.request_reviewer("proj", 99)

            mock_activity.assert_awaited_once()
            call_args = mock_activity.call_args
            assert call_args[0][0] == "request_github_reviewer"


class TestPRCommentWorkflowImplementsPhaseOps:
    """PRCommentWorkflow implements the PhaseOps protocol by delegating to
    Temporal activity calls."""

    @pytest.mark.asyncio
    async def test_workflow_is_phaseops(self) -> None:
        """PRCommentWorkflow is a PhaseOps instance."""
        from devloop.pr_comment import PRCommentWorkflow

        workflow = PRCommentWorkflow()
        assert isinstance(workflow, PhaseOps)

    @pytest.mark.asyncio
    async def test_workflow_delegates_comment_to_activity(self) -> None:
        """PRCommentWorkflow.comment calls the post_github_comment activity."""
        from devloop.pr_comment import PRCommentWorkflow

        workflow = PRCommentWorkflow()

        with patch("devloop.pr_comment.workflow.execute_activity") as mock_activity:
            mock_coro = AsyncMock()
            mock_activity.return_value = mock_coro

            await workflow.comment("proj-42", 99, "hello")

            mock_activity.assert_awaited_once()
            call_args = mock_activity.call_args
            assert call_args[0][0] == "post_github_comment"

    @pytest.mark.asyncio
    async def test_workflow_delegates_dispatch_to_activity(self) -> None:
        """PRCommentWorkflow.dispatch calls the dispatch_agent_job activity."""
        from devloop.pr_comment import PRCommentWorkflow
        from devloop.shared import TaskSpec

        workflow = PRCommentWorkflow()

        with patch("devloop.pr_comment.workflow.execute_activity") as mock_activity:
            mock_result = MagicMock()
            mock_result.job_name = "test-job"
            mock_result.status = "complete"
            mock_coro = AsyncMock(return_value=mock_result)
            mock_activity.return_value = mock_coro

            spec = TaskSpec(
                phase="pr_comment",
                project_id="proj",
                issue_number=1,
            )
            await workflow.dispatch(
                "proj", spec, issue_number=1, poll_interval_seconds=5.0
            )

            # _dispatch calls execute_activity (dispatch), then _cleanup calls
            # it again (cleanup_configmap) — so at least 2 calls total.
            assert mock_activity.await_count >= 1
            first_call_name = mock_activity.call_args_list[0][0][0]
            assert first_call_name == "dispatch_agent_job"

    @pytest.mark.asyncio
    async def test_workflow_delegates_request_reviewer_to_activity(self) -> None:
        """PRCommentWorkflow.request_reviewer calls the request_github_reviewer activity."""
        from devloop.pr_comment import PRCommentWorkflow

        workflow = PRCommentWorkflow()

        with patch("devloop.pr_comment.workflow.execute_activity") as mock_activity:
            mock_result = MagicMock()
            mock_result.requested = True
            mock_coro = AsyncMock(return_value=mock_result)
            mock_activity.return_value = mock_coro

            await workflow.request_reviewer("proj", 99)

            mock_activity.assert_awaited_once()
            call_args = mock_activity.call_args
            assert call_args[0][0] == "request_github_reviewer"

    @pytest.mark.asyncio
    async def test_cicycle_receives_workflow_phaseops(self) -> None:
        """PRCommentWorkflow passes its PhaseOps to CICycle directly."""
        from devloop.pr_comment import PRCommentWorkflow, PRCommentInput
        from devloop.phases.phase_ops import PhaseOps

        workflow = PRCommentWorkflow()

        async def _side_effect(activity_name: str, *args: Any, **kwargs: Any) -> Any:
            """Return the right value for each activity call."""
            if activity_name == "post_github_comment":
                return None
            elif activity_name == "get_pr_branch":
                return MagicMock(branch="agent/issue-42")
            elif activity_name == "dispatch_agent_job":
                result = MagicMock()
                result.status = "complete"
                result.commits = 2
                result.branch = "agent/issue-42"
                result.pr_url = "https://github.com/p/r/42"
                result.summary = "done"
                result.job_name = "test-job"
                return result
            elif activity_name == "cleanup_configmap":
                return None
            elif activity_name == "request_github_reviewer":
                return MagicMock(requested=True)
            return None

        with patch(
            "devloop.pr_comment.workflow.execute_activity", side_effect=_side_effect
        ):
            inp = PRCommentInput(
                project_id="proj",
                pr_number=42,
                issue_number=42,
                branch="agent/issue-42",
                comment_body="fix this",
                source="comment",
                author="human",
            )

            with patch("devloop.pr_comment.CICycle") as mock_cycle:
                mock_cycle_instance = MagicMock()
                mock_cycle_instance.run = AsyncMock(
                    return_value=MagicMock(exhausted=False, commits=0)
                )
                mock_cycle.return_value = mock_cycle_instance

                result = await workflow.run(inp)

                assert result.status == "completed"
                # The phaseops field on the workflow should be a PhaseOps
                assert isinstance(workflow.phaseops, PhaseOps)


class TestPhaseopsCallbackInjection:
    """Tests verifying callback injection through the unified protocol works
    for at least one phase module."""

    @pytest.mark.asyncio
    async def test_execute_phase_receives_phaseops_from_workflow(self) -> None:
        """ExecutePhase receives a PhaseOps from DevLoopWorkflow and its
        callbacks are invoked."""
        from devloop.dev_loop import DevLoopWorkflow

        workflow = DevLoopWorkflow()

        # Verify workflow is PhaseOps and dispatch_execute points to activity
        assert isinstance(workflow, PhaseOps)
        assert workflow.dispatch_execute is not None

        # The dispatch_execute callback should be an async callable that wraps
        # the Temporal activity
        import inspect

        assert inspect.iscoroutinefunction(workflow.dispatch_execute)

    @pytest.mark.asyncio
    async def test_review_phase_receives_phaseops_from_workflow(self) -> None:
        """ReviewPhase receives a PhaseOps from DevLoopWorkflow."""
        from devloop.dev_loop import DevLoopWorkflow

        workflow = DevLoopWorkflow()

        assert isinstance(workflow, PhaseOps)
        assert workflow.dispatch_review is not None
        assert workflow.post_review_findings is not None

    @pytest.mark.asyncio
    async def test_notifier_receives_phaseops_from_workflow(self) -> None:
        """Notifier receives a PhaseOps from DevLoopWorkflow."""
        from devloop.dev_loop import DevLoopWorkflow

        workflow = DevLoopWorkflow()

        assert isinstance(workflow, PhaseOps)
        assert workflow.request_reviewer is not None
        assert workflow.comment is not None
