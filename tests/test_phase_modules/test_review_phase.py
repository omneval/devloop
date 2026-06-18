"""Unit tests for devloop.phases.review — ReviewPhase standalone module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from devloop.phases.review import ReviewPhase, ReviewPhaseCallbacks
from devloop.shared import AgentJobResult


class TestReviewPhase:
    """ReviewPhase — review the PR and post findings."""

    @pytest.mark.asyncio
    async def test_review_with_verdict(self) -> None:
        """ReviewPhase posts comment and returns review when verdict exists."""
        phase = ReviewPhase()
        review_result = {
            "verdict": "needs_fixes",
            "summary": "Missing test coverage",
        }
        callbacks = ReviewPhaseCallbacks(
            dispatch_review=AsyncMock(
                return_value=AgentJobResult(
                    status="complete",
                    review=review_result,
                )
            ),
            post_review_findings=AsyncMock(),
            post_comment=AsyncMock(),
        )
        inp = MagicMock(project_id="proj", poll_interval_seconds=5.0)
        issue = {"id": "42"}
        exec_result = {
            "branch": "feat/1",
            "pr_url": "https://github.com/org/repo/pull/1",
        }

        result = await phase.run(
            inp=inp,
            issue=issue,
            exec_result=exec_result,
            callbacks=callbacks,
        )

        assert result["verdict"] == "needs_fixes"
        # Should have been called: queued comment + verdict comment + findings
        assert callbacks.post_comment.await_count >= 2

    @pytest.mark.asyncio
    async def test_review_no_verdict(self) -> None:
        """ReviewPhase returns review dict when review has content but no verdict."""
        phase = ReviewPhase()
        callbacks = ReviewPhaseCallbacks(
            dispatch_review=AsyncMock(
                return_value=AgentJobResult(
                    status="complete",
                    review={"summary": "no changes needed"},
                )
            ),
            post_review_findings=AsyncMock(),
            post_comment=AsyncMock(),
        )
        inp = MagicMock(project_id="proj", poll_interval_seconds=5.0)
        issue = {"id": "42"}
        exec_result = {
            "branch": "feat/1",
            "pr_url": "https://github.com/org/repo/pull/1",
        }

        result = await phase.run(
            inp=inp,
            issue=issue,
            exec_result=exec_result,
            callbacks=callbacks,
        )

        # When review has content but no verdict, returns the dict (not None)
        assert result == {"summary": "no changes needed"}
        assert result.get("verdict") is None

    @pytest.mark.asyncio
    async def test_review_none_result(self) -> None:
        """ReviewPhase returns None when review attribute is missing."""
        phase = ReviewPhase()
        callbacks = ReviewPhaseCallbacks(
            dispatch_review=AsyncMock(
                return_value=AgentJobResult(
                    status="complete",
                    review=None,
                )
            ),
            post_review_findings=AsyncMock(),
            post_comment=AsyncMock(),
        )
        inp = MagicMock(project_id="proj", poll_interval_seconds=5.0)
        issue = {"id": "42"}
        exec_result = {
            "branch": "feat/1",
            "pr_url": "https://github.com/org/repo/pull/1",
        }

        result = await phase.run(
            inp=inp,
            issue=issue,
            exec_result=exec_result,
            callbacks=callbacks,
        )

        assert result is None


class TestReviewPhasePostCommentsRetry:
    """post_pr_comments — must use single-attempt retry to prevent duplicates."""

    @pytest.mark.asyncio
    async def test_post_pr_comments_uses_single_attempt_retry(self) -> None:
        """The post_pr_comments activity must use maximum_attempts=1 so that
        a retry never creates duplicate GitHub comments.

        Regression test for issue #173: the review agent was posting the same
        comment 3 times because the activity used ``_RETRY`` (3 attempts).
        """
        phase = ReviewPhase()

        mock_result = MagicMock(spec=AgentJobResult)
        mock_result.review = {
            "verdict": "approved",
            "summary": "All good",
            "inline_comments": [],
        }
        mock_result.job_name = "review-1"
        mock_result.status = "complete"
        mock_result.commits = 0

        callbacks = ReviewPhaseCallbacks(
            dispatch_review=AsyncMock(return_value=mock_result),
            # Leave post_review_findings=None so the Temporal activity path
            # is exercised (post_pr_comments).
            post_review_findings=None,
            post_comment=AsyncMock(),
        )
        inp = MagicMock(project_id="omneval", poll_interval_seconds=5.0)
        issue = {"id": "42", "title": "Add a feature"}
        exec_result = {
            "branch": "agent/issue-42",
            "pr_url": "https://github.com/omneval/omneval/pull/42",
        }

        with patch("devloop.phases.review.workflow.execute_activity") as mock_execute:
            mock_execute.side_effect = [None, None]

            await phase.run(inp, issue, exec_result, callbacks)

            post_pr_comments_calls = [
                c
                for c in mock_execute.call_args_list
                if c[0] and c[0][0] == "post_pr_comments"
            ]
            assert len(post_pr_comments_calls) >= 1

            for call in post_pr_comments_calls:
                retry_policy = call.kwargs.get("retry_policy")
                assert retry_policy is not None
                assert retry_policy.maximum_attempts == 1, (
                    f"post_pr_comments activity must use maximum_attempts=1 "
                    f"to prevent duplicate GitHub comments, got "
                    f"{retry_policy.maximum_attempts}"
                )
