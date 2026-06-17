"""Unit tests for devloop.phases.review_fix_pass — ReviewFixPass standalone module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from devloop.phases.review_fix_pass import ReviewFixPass, ReviewFixPassCallbacks


class TestReviewFixPass:
    """ReviewFixPass — one fix pass after a review's findings."""

    @pytest.mark.asyncio
    async def test_fix_pass_produces_commits(self) -> None:
        """ReviewFixPass returns True when fix produces commits."""
        phase = ReviewFixPass()
        callbacks = ReviewFixPassCallbacks(
            dispatch_fix=AsyncMock(return_value=MagicMock(commits=2, branch="feat/1")),
            post_comment=AsyncMock(),
        )
        inp = MagicMock(poll_interval_seconds=5.0)
        issue = {"id": "42"}
        exec_result = {"pr_url": "https://github.com/p/r/1", "branch": "feat/1"}
        review = {"summary": "Missing tests"}

        result = await phase.run(
            inp=inp,
            issue=issue,
            exec_result=exec_result,
            review=review,
            callbacks=callbacks,
        )

        assert result is True
        callbacks.post_comment.assert_awaited()

    @pytest.mark.asyncio
    async def test_fix_pass_no_findings_returns_false(self) -> None:
        """ReviewFixPass returns False when review has no findings."""
        phase = ReviewFixPass()
        callbacks = ReviewFixPassCallbacks(
            dispatch_fix=AsyncMock(),
            post_comment=AsyncMock(),
        )
        inp = MagicMock(poll_interval_seconds=5.0)
        issue = {"id": "42"}
        exec_result = {"pr_url": "https://github.com/p/r/1", "branch": "feat/1"}
        review = {"summary": ""}

        result = await phase.run(
            inp=inp,
            issue=issue,
            exec_result=exec_result,
            review=review,
            callbacks=callbacks,
        )

        assert result is False
        callbacks.dispatch_fix.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fix_pass_no_commits_returns_false(self) -> None:
        """ReviewFixPass returns False when fix produces zero commits."""
        phase = ReviewFixPass()
        callbacks = ReviewFixPassCallbacks(
            dispatch_fix=AsyncMock(return_value=MagicMock(commits=0, branch="")),
            post_comment=AsyncMock(),
        )
        inp = MagicMock(poll_interval_seconds=5.0)
        issue = {"id": "42"}
        exec_result = {"pr_url": "https://github.com/p/r/1", "branch": "feat/1"}
        review = {"summary": "Missing tests"}

        result = await phase.run(
            inp=inp,
            issue=issue,
            exec_result=exec_result,
            review=review,
            callbacks=callbacks,
        )

        assert result is False
