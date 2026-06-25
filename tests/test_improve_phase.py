"""Tests for ImprovePhase — the improvement dispatch phase of CodeQualityWorkflow.

Follows the same pattern as test_scanning_phase.py: exercise the public
``run()`` interface with injected callbacks.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from devloop.phases.improve_phase import ImprovePhase, ImprovePhaseCallbacks


class TestImprovePhaseRun:
    """Tests for ImprovePhase.run() with injected callbacks."""

    @pytest.mark.asyncio
    async def test_dispatches_improve_and_returns_summary(self):
        """ImprovePhase dispatches the improve phase and returns the summary."""
        callbacks = ImprovePhaseCallbacks()
        callbacks.dispatch = AsyncMock()
        callbacks.post_comment = AsyncMock()

        dispatch_result = MagicMock()
        dispatch_result.status = "complete"
        dispatch_result.summary = "filed 3 issues"
        callbacks.dispatch.return_value = dispatch_result

        phase = ImprovePhase()
        result = await phase.run(
            project_id="myrepo",
            report="many issues",
            parent_issue_number=42,
            agent_label="agent-ready",
            callbacks=callbacks,
        )

        callbacks.dispatch.assert_called_once()
        call_args = callbacks.dispatch.call_args[0]
        assert call_args[0] == "myrepo"
        assert call_args[1].phase == "code_quality_improve"
        assert call_args[1].extra["sentrux_report"] == "many issues"
        assert call_args[1].extra["parent_issue_number"] == 42
        assert call_args[1].extra["agent_label"] == "agent-ready"

        assert result["summary"] == "filed 3 issues"
        assert result["parent_issue_number"] == 42

    @pytest.mark.asyncio
    async def test_posts_completion_comment(self):
        """ImprovePhase posts a queued comment and a completion comment."""
        callbacks = ImprovePhaseCallbacks()
        callbacks.dispatch = AsyncMock()
        callbacks.post_comment = AsyncMock()

        dispatch_result = MagicMock()
        dispatch_result.status = "complete"
        dispatch_result.summary = "filed 5 issues"
        callbacks.dispatch.return_value = dispatch_result

        phase = ImprovePhase()
        await phase.run(
            project_id="myrepo",
            report="many issues",
            parent_issue_number=42,
            agent_label="agent-ready",
            callbacks=callbacks,
        )

        post_comment_calls = callbacks.post_comment.call_args_list
        # Should have queued comment + completion comment
        assert len(post_comment_calls) == 2
        queued = post_comment_calls[0][0][2]
        assert "queued" in queued.lower()
        completion = post_comment_calls[1][0][2]
        assert "filed" in completion.lower() or "issues" in completion.lower()

    @pytest.mark.asyncio
    async def test_handles_empty_summary(self):
        """ImprovePhase handles empty summary gracefully."""
        callbacks = ImprovePhaseCallbacks()
        callbacks.dispatch = AsyncMock()
        callbacks.post_comment = AsyncMock()

        dispatch_result = MagicMock()
        dispatch_result.status = "complete"
        dispatch_result.summary = ""
        callbacks.dispatch.return_value = dispatch_result

        phase = ImprovePhase()
        result = await phase.run(
            project_id="myrepo",
            report="minor issues",
            parent_issue_number=99,
            agent_label="agent-v2",
            callbacks=callbacks,
        )

        assert result["summary"] == ""
        assert result["parent_issue_number"] == 99
