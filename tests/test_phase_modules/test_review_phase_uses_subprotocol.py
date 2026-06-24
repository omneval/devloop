"""Tests verifying ReviewPhase uses its ReviewPhaseOps sub-protocol."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from devloop.phases.review import ReviewPhase
from devloop.phases.phase_ops import PhaseOps
from devloop.execution import AgentJobResult


class TestReviewPhaseUsesReviewOpsSubProtocol:
    """ReviewPhase must use its focused ReviewPhaseOps sub-protocol."""

    @pytest.mark.asyncio
    async def test_uses_review_ops_for_comment(self) -> None:
        """ReviewPhase accesses comment via review_ops sub-protocol."""
        phase = ReviewPhase()

        callbacks = PhaseOps(
            dispatch_review=AsyncMock(
                return_value=AgentJobResult(
                    status="complete",
                    review={"verdict": "approved"},
                )
            ),
        )
        # Set review_ops.comment so the phase can call it.
        callbacks.review_ops.comment = AsyncMock()

        inp = MagicMock(project_id="proj", poll_interval_seconds=5.0)
        issue = {"id": "42"}
        exec_result = {
            "branch": "feat/1",
            "pr_url": "https://github.com/org/repo/pull/1",
        }

        await phase.run(
            inp=inp,
            issue=issue,
            exec_result=exec_result,
            callbacks=callbacks,
        )

        # review_ops.comment must have been called.
        assert callbacks.review_ops.comment is not None
        callbacks.review_ops.comment.assert_awaited()

    @pytest.mark.asyncio
    async def test_uses_review_ops_for_dispatch_review(self) -> None:
        """ReviewPhase accesses dispatch_review via review_ops sub-protocol."""
        phase = ReviewPhase()

        async def _dispatch_review(
            project_id: str, spec: Any, issue_number: int, poll: float
        ) -> Any:
            _dispatch_review._called = (project_id, issue_number)
            return AgentJobResult(
                status="complete",
                review={"verdict": "approved"},
            )

        _dispatch_review._called = False  # type: ignore[attr-defined]

        async def _noop_comment(*a: Any, **kw: Any) -> None:
            pass

        callbacks = PhaseOps()
        callbacks.review_ops.comment = _noop_comment
        callbacks.review_ops.dispatch_review = _dispatch_review  # type: ignore[assignment]

        inp = MagicMock(project_id="proj", poll_interval_seconds=5.0)
        issue = {"id": "42"}
        exec_result = {
            "branch": "feat/1",
            "pr_url": "https://github.com/org/repo/pull/1",
        }

        await phase.run(
            inp=inp,
            issue=issue,
            exec_result=exec_result,
            callbacks=callbacks,
        )

        assert callbacks.review_ops.dispatch_review is not None
        assert _dispatch_review._called == ("proj", 42)  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_uses_review_ops_for_post_review_findings(self) -> None:
        """ReviewPhase accesses post_review_findings via review_ops sub-protocol."""
        phase = ReviewPhase()

        async def _post_review(*a: Any, **kw: Any) -> None:
            _post_review._called = True

        _post_review._called = False  # type: ignore[attr-defined]

        async def _noop_comment(*a: Any, **kw: Any) -> None:
            pass

        callbacks = PhaseOps(
            dispatch_review=AsyncMock(
                return_value=AgentJobResult(
                    status="complete",
                    review={"verdict": "approved"},
                )
            ),
        )
        callbacks.review_ops.comment = _noop_comment
        callbacks.review_ops.post_review_findings = _post_review  # type: ignore[assignment]

        inp = MagicMock(project_id="proj", poll_interval_seconds=5.0)
        issue = {"id": "42"}
        exec_result = {
            "branch": "feat/1",
            "pr_url": "https://github.com/org/repo/pull/1",
        }

        await phase.run(
            inp=inp,
            issue=issue,
            exec_result=exec_result,
            callbacks=callbacks,
        )

        assert callbacks.review_ops.post_review_findings is not None
        assert _post_review._called is True  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_review_ops_fallback_uses_phaseops_field(self) -> None:
        """When review_ops.comment is None, ReviewPhase falls back to PhaseOps.comment."""
        phase = ReviewPhase()

        callbacks = PhaseOps(
            dispatch_review=AsyncMock(
                return_value=AgentJobResult(
                    status="complete",
                    review={"verdict": "approved"},
                )
            ),
            post_comment=AsyncMock(),
        )
        # review_ops.comment is None (default), so it should fall back.
        callbacks.review_ops.comment = None

        inp = MagicMock(project_id="proj", poll_interval_seconds=5.0)
        issue = {"id": "42"}
        exec_result = {
            "branch": "feat/1",
            "pr_url": "https://github.com/org/repo/pull/1",
        }

        await phase.run(
            inp=inp,
            issue=issue,
            exec_result=exec_result,
            callbacks=callbacks,
        )

        # PhaseOps.post_comment should have been called as fallback.
        callbacks.post_comment.assert_awaited()
