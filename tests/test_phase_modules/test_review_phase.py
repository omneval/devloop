"""Unit tests for devloop.phases.review — ReviewPhase standalone module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from devloop.phases.review import ReviewPhase
from devloop.phases.phase_ops import PhaseOps
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
        callbacks = PhaseOps(
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
        callbacks = PhaseOps(
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
        callbacks = PhaseOps(
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
