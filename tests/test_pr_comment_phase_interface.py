"""Tests verifying PRCommentPhase.run() uses PRCommentInput -> PRCommentResult."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import pytest

from devloop.phases.pr_comment import PRCommentPhase
from devloop.pr_comment import PRCommentInput, PRCommentResult
from devloop.shared import AgentJobResult, JobStatus


@dataclass
class PhaseMocks:
    """Mock callbacks for PRCommentPhase tests."""

    post_comment_calls: list = field(default_factory=list)
    get_branch_calls: list = field(default_factory=list)
    dispatch_calls: list = field(default_factory=list)

    branch_result: str = "agent/issue-53"
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


class TestPRCommentPhaseInterface:
    """Verify PRCommentPhase.run() uses PRCommentInput -> PRCommentResult."""

    @pytest.mark.asyncio
    async def test_run_signature_accepts_pr_comment_input(
        self, mocks: PhaseMocks
    ) -> None:
        """PRCommentPhase.run() must accept inp: PRCommentInput."""
        phase = PRCommentPhase()
        sig = inspect.signature(phase.run)
        params = list(sig.parameters.keys())
        assert "inp" in params

    @pytest.mark.asyncio
    async def test_run_returns_pr_comment_result_type(self, mocks: PhaseMocks) -> None:
        """PRCommentPhase.run() returns PRCommentResult, not a custom type."""
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

        # The result must be PRCommentResult, NOT a custom PRCommentPhaseResult
        assert isinstance(result, PRCommentResult)

    @pytest.mark.asyncio
    async def test_run_produces_exec_result_in_pr_comment_result(
        self, mocks: PhaseMocks
    ) -> None:
        """PRCommentResult carries exec_result dict with issue_id, branch, pr_url, commits."""
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

        assert isinstance(result, PRCommentResult)
        # exec_result dict should be present
        assert result.exec_result is not None
        assert result.exec_result["issue_id"] == 53
        assert result.exec_result["branch"] == "agent/issue-53"
        assert (
            result.exec_result["pr_url"] == "https://github.com/omneval/omneval/pull/17"
        )
        assert result.exec_result["commits"] == 1
        assert result.error is None

    @pytest.mark.asyncio
    async def test_run_error_path_sets_exec_result_none(
        self, mocks: PhaseMocks
    ) -> None:
        """When dispatch fails, exec_result is None and error is set."""
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

        assert isinstance(result, PRCommentResult)
        assert result.exec_result is None
        assert result.error == "task_queue not found"

    @pytest.mark.asyncio
    async def test_run_fails_on_empty_branch_resolution(
        self, mocks: PhaseMocks
    ) -> None:
        """When branch resolution returns empty, exec_result is None."""
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

        assert isinstance(result, PRCommentResult)
        assert result.exec_result is None
        assert result.error == "branch resolution failed"

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

        assert isinstance(result, PRCommentResult)
        assert result.exec_result is None
        assert result.error == "not an agent-owned branch"
