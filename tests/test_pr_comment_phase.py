"""Tests for PRCommentPhase — the deep module extracted from PRCommentWorkflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import pytest

from devloop.phases.pr_comment import PRCommentPhase, PRCommentPhaseResult
from devloop.pr_comment import PRCommentInput
from devloop.shared import AgentJobResult, JobStatus


@dataclass
class PhaseMocks:
    """Mock callbacks for PRCommentPhase tests."""

    post_comment_calls: list = field(default_factory=list)
    get_branch_calls: list = field(default_factory=list)
    dispatch_calls: list = field(default_factory=list)

    # Default branch resolution result
    branch_result: str = "agent/issue-53"
    # Default dispatch result
    dispatch_result: AgentJobResult = field(
        default_factory=lambda: AgentJobResult(
            status=JobStatus.COMPLETE.value,
            job_name="pr-comment-job",
            issue_number=53,
            branch="agent/issue-53",
            pr_url="https://github.com/omneval/omneval/pull/17",
            commits=1,
            summary="Pushed `abc1234`: renamed the helper per feedback.",
        )
    )


@pytest.fixture
def mocks() -> PhaseMocks:
    return PhaseMocks()


def _input(**overrides):
    base = dict(
        project_id="omneval",
        pr_number=17,
        issue_number=53,
        branch="agent/issue-53",
        comment_body="Please rename this function.",
        source="review",
        author="a-human-reviewer",
        poll_interval_seconds=5.0,
    )
    base.update(overrides)
    return PRCommentInput(**base)


class TestPRCommentPhase:
    """Tests for PRCommentPhase.run() via callback injection."""

    @pytest.mark.asyncio
    async def test_run_with_branch_provided_happy_path(self, mocks: PhaseMocks) -> None:
        """When branch is provided, dispatches Phase.PR_COMMENT and returns
        the agent job result."""
        phase = PRCommentPhase()

        mock_post_comment = AsyncMock()
        mock_get_branch = AsyncMock()
        mock_dispatch = AsyncMock()

        from devloop.phases.pr_comment import _Callbacks

        callbacks = _Callbacks(
            post_comment=mock_post_comment,
            get_branch=mock_get_branch,
            dispatch=mock_dispatch,
        )

        mock_dispatch.return_value = mocks.dispatch_result

        result = await phase.run(_input(), callbacks=callbacks)

        assert isinstance(result, PRCommentPhaseResult)
        assert result.exec_result is not None
        assert result.exec_result["issue_id"] == 53
        assert result.exec_result["branch"] == "agent/issue-53"
        assert (
            result.exec_result["pr_url"] == "https://github.com/omneval/omneval/pull/17"
        )
        assert result.exec_result["commits"] == 1
        assert result.error is None
        assert mock_get_branch.call_count == 0  # branch provided, no resolution needed
        assert mock_dispatch.call_count == 1

    @pytest.mark.asyncio
    async def test_run_resolves_branch_when_empty(self, mocks: PhaseMocks) -> None:
        """When branch is empty, calls get_branch callback to resolve it."""
        phase = PRCommentPhase()

        mock_post_comment = AsyncMock()
        mock_get_branch = AsyncMock()
        mock_dispatch = AsyncMock()

        from devloop.phases.pr_comment import _Callbacks

        callbacks = _Callbacks(
            post_comment=mock_post_comment,
            get_branch=mock_get_branch,
            dispatch=mock_dispatch,
        )

        mock_get_branch.return_value = "agent/issue-53-feature"
        mock_dispatch.return_value = AgentJobResult(
            status=JobStatus.COMPLETE.value,
            job_name="pr-comment-job",
            issue_number=53,
            branch="agent/issue-53-feature",
            pr_url="https://github.com/omneval/omneval/pull/17",
            commits=1,
            summary="Done",
        )

        result = await phase.run(_input(branch=""), callbacks=callbacks)

        assert result.exec_result is not None
        assert result.exec_result["branch"] == "agent/issue-53-feature"
        mock_get_branch.assert_called_once_with("omneval", 17)
        assert mock_dispatch.call_count == 1

    @pytest.mark.asyncio
    async def test_run_fails_when_branch_resolution_returns_empty(
        self, mocks: PhaseMocks
    ) -> None:
        """When get_branch returns empty, returns error without dispatching."""
        phase = PRCommentPhase()

        mock_post_comment = AsyncMock()
        mock_get_branch = AsyncMock()
        mock_dispatch = AsyncMock()

        from devloop.phases.pr_comment import _Callbacks

        callbacks = _Callbacks(
            post_comment=mock_post_comment,
            get_branch=mock_get_branch,
            dispatch=mock_dispatch,
        )

        mock_get_branch.return_value = ""

        result = await phase.run(_input(branch=""), callbacks=callbacks)

        assert result.exec_result is None
        assert result.error == "branch resolution failed"
        assert mock_dispatch.call_count == 0

    @pytest.mark.asyncio
    async def test_run_refuses_non_agent_branch(self, mocks: PhaseMocks) -> None:
        """A branch not matching agent/issue-<N> is refused."""
        phase = PRCommentPhase()

        mock_post_comment = AsyncMock()
        mock_get_branch = AsyncMock()
        mock_dispatch = AsyncMock()

        from devloop.phases.pr_comment import _Callbacks

        callbacks = _Callbacks(
            post_comment=mock_post_comment,
            get_branch=mock_get_branch,
            dispatch=mock_dispatch,
        )

        mock_get_branch.return_value = "a-humans-feature-branch"

        result = await phase.run(_input(branch=""), callbacks=callbacks)

        assert result.exec_result is None
        assert result.error == "not an agent-owned branch"
        assert mock_dispatch.call_count == 0

    @pytest.mark.asyncio
    async def test_run_dispatch_fails(self, mocks: PhaseMocks) -> None:
        """When dispatch returns a failed status, returns error."""
        phase = PRCommentPhase()

        mock_post_comment = AsyncMock()
        mock_get_branch = AsyncMock()
        mock_dispatch = AsyncMock()

        from devloop.phases.pr_comment import _Callbacks

        callbacks = _Callbacks(
            post_comment=mock_post_comment,
            get_branch=mock_get_branch,
            dispatch=mock_dispatch,
        )

        failed_result = AgentJobResult(
            status=JobStatus.FAILED.value,
            job_name="pr-comment-job",
            issue_number=53,
            error="task_queue not found",
        )
        mock_dispatch.return_value = failed_result

        result = await phase.run(_input(), callbacks=callbacks)

        assert result.exec_result is None
        assert result.error == "task_queue not found"
