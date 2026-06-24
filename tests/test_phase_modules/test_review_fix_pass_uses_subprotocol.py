"""Tests verifying ReviewFixPass uses its CICycleOps sub-protocol."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from devloop.phases.review_fix_pass import ReviewFixPass, PhaseOps


class TestReviewFixPassUsesCICycleOpsSubProtocol:
    """ReviewFixPass must use its focused CICycleOps sub-protocol."""

    @pytest.mark.asyncio
    async def test_uses_ci_ops_for_comment(self) -> None:
        """ReviewFixPass accesses comment via ci_ops sub-protocol."""
        phase = ReviewFixPass()

        callbacks = PhaseOps(
            dispatch_fix=AsyncMock(return_value=2),
        )
        # Set ci_ops.comment so the phase can call it.
        callbacks.ci_ops.comment = AsyncMock()

        inp = MagicMock(poll_interval_seconds=5.0, project_id="test")
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
        assert callbacks.ci_ops.comment is not None
        callbacks.ci_ops.comment.assert_awaited()

    @pytest.mark.asyncio
    async def test_uses_ci_ops_for_dispatch_fix(self) -> None:
        """ReviewFixPass accesses dispatch_fix via ci_ops sub-protocol."""
        phase = ReviewFixPass()

        async def _dispatch_fix(
            project_id: str, spec: Any, issue_number: int, poll: float
        ) -> int:
            _dispatch_fix._called = (project_id, issue_number)
            return 2

        _dispatch_fix._called = False  # type: ignore[attr-defined]

        callbacks = PhaseOps()
        callbacks.ci_ops.comment = AsyncMock()
        callbacks.ci_ops.dispatch_fix = _dispatch_fix  # type: ignore[assignment]

        inp = MagicMock(poll_interval_seconds=5.0, project_id="test")
        issue = {"id": "42"}
        exec_result = {"pr_url": "https://github.com/p/r/1", "branch": "feat/1"}
        review = {"summary": "Missing tests"}

        await phase.run(
            inp=inp,
            issue=issue,
            exec_result=exec_result,
            review=review,
            callbacks=callbacks,
        )

        assert _dispatch_fix._called == ("test", 42)  # type: ignore[attr-defined]
        assert callbacks.ci_ops.dispatch_fix is not None

    @pytest.mark.asyncio
    async def test_ci_ops_fallback_uses_phaseops_field(self) -> None:
        """When ci_ops.comment is None, ReviewFixPass falls back to PhaseOps.comment."""
        phase = ReviewFixPass()

        callbacks = PhaseOps(
            dispatch_fix=AsyncMock(return_value=2),
            post_comment=AsyncMock(),
        )
        # ci_ops.comment is None (default), so it should fall back.
        callbacks.ci_ops.comment = None

        inp = MagicMock(poll_interval_seconds=5.0, project_id="test")
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
