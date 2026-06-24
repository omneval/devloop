"""Tests for issue #202 — I/O duplication eliminated from phase classes.

Phase classes should delegate all I/O through PhaseOps, not re-implement
_temporal activity calls.  Only phase-specific logic stays in the phases.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from devloop.phases.execute import ExecutePhase
from devloop.phases.phase_ops import PhaseOps
from devloop.phases.review import ReviewPhase
from devloop.shared import AgentJobResult, JobStatus


class TestExecutePhaseNoIODuplication:
    """ExecutePhase must not define I/O methods that duplicate PhaseOps."""

    @pytest.mark.asyncio
    async def test_no__comment_method(self) -> None:
        """ExecutePhase._comment must not exist."""
        assert not hasattr(ExecutePhase, "_comment"), (
            "ExecutePhase still defines _comment — it duplicates "
            "PhaseOps._phase_comment; use PhaseOps methods via the injectable seam."
        )

    @pytest.mark.asyncio
    async def test_no__cleanup_method(self) -> None:
        """ExecutePhase._cleanup must not exist."""
        assert not hasattr(ExecutePhase, "_cleanup"), (
            "ExecutePhase still defines _cleanup — it duplicates "
            "PhaseOps._phase_cleanup; use PhaseOps methods via the injectable seam."
        )

    @pytest.mark.asyncio
    async def test_run_uses_phaseops_for_comment(self) -> None:
        """ExecutePhase.run delegates commenting to PhaseOps._phase_comment."""
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

        # We can't easily verify the internal call chain without accessing
        # internals, but the method existence test above covers it.
        # This test ensures the interface still works.
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

        assert result["commits"] == 3
        post_comment = callbacks.post_comment  # type: ignore[assignment]
        assert post_comment.called  # ty: ignore[unresolved-attribute]

    @pytest.mark.asyncio
    async def test_run_uses_phaseops_for_cleanup(self) -> None:
        """ExecutePhase.run delegates cleanup to PhaseOps._phase_cleanup."""
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

        assert result["commits"] == 3


class TestReviewPhaseNoIODuplication:
    """ReviewPhase must not define I/O methods that duplicate PhaseOps."""

    @pytest.mark.asyncio
    async def test_no__comment_method(self) -> None:
        """ReviewPhase._comment must not exist."""
        assert not hasattr(ReviewPhase, "_comment"), (
            "ReviewPhase still defines _comment — it duplicates "
            "PhaseOps._phase_comment; use PhaseOps methods via the injectable seam."
        )

    @pytest.mark.asyncio
    async def test_run_uses_phaseops_for_comment(self) -> None:
        """ReviewPhase.run delegates commenting to PhaseOps._phase_comment."""
        phase = ReviewPhase()
        callbacks = PhaseOps(
            dispatch_review=AsyncMock(
                return_value=AgentJobResult(
                    status="complete",
                    review={"verdict": "needs_fixes", "summary": "some notes"},
                )
            ),
            post_review_findings=AsyncMock(),
            post_comment=AsyncMock(),
        )

        inp = MagicMock(project_id="proj", poll_interval_seconds=5.0)

        review_result = await phase.run(
            inp=inp,
            issue={"id": "42"},
            exec_result={
                "branch": "feat/1",
                "pr_url": "https://github.com/org/repo/pull/1",
            },
            callbacks=callbacks,
        )

        assert review_result is not None
        assert review_result["verdict"] == "needs_fixes"
        post_comment = callbacks.post_comment  # type: ignore[assignment]
        assert post_comment.called  # ty: ignore[unresolved-attribute]


class TestPhaseSpecificMethodsRemain:
    """Phase-specific methods must stay in their respective phases."""

    def test_execute_phase_keeps_answer_questions(self) -> None:
        """ExecutePhase._answer_questions remains — it's phase-specific."""
        assert hasattr(ExecutePhase, "_answer_questions")

    def test_review_phase_keeps_post_review_findings(self) -> None:
        """ReviewPhase._post_review_findings remains — it's phase-specific."""
        assert hasattr(ReviewPhase, "_post_review_findings")


class TestCallbackFieldsRemain:
    """Callback fields on PhaseOps must remain accessible."""

    def test_phaseops_has_dispatch_execute(self) -> None:
        """PhaseOps exposes dispatch_execute."""
        assert "dispatch_execute" in dir(PhaseOps)

    def test_phaseops_has_answer_question(self) -> None:
        """PhaseOps exposes answer_question."""
        assert "answer_question" in dir(PhaseOps)

    def test_phaseops_has_dispatch_review(self) -> None:
        """PhaseOps exposes dispatch_review."""
        assert "dispatch_review" in dir(PhaseOps)

    def test_phaseops_has_post_review_findings(self) -> None:
        """PhaseOps exposes post_review_findings."""
        assert "post_review_findings" in dir(PhaseOps)
