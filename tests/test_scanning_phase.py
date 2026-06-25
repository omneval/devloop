"""Tests for ScanningPhase — the scan dispatch phase of CodeQualityWorkflow.

Follows the same pattern as test_pr_comment_phase.py: exercise the public
``run()`` interface with injected callbacks.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from devloop.phases.scanning_phase import ScanningPhase, ScanningPhaseCallbacks


class TestScanningPhaseRun:
    """Tests for ScanningPhase.run() with injected callbacks."""

    @pytest.mark.asyncio
    async def test_dispatches_scan_and_returns_result(self):
        """ScanningPhase dispatches the scan and returns the deserialized result."""
        # Set up callbacks
        callbacks = ScanningPhaseCallbacks()
        callbacks.dispatch = AsyncMock()
        callbacks.post_comment = AsyncMock()
        callbacks.create_issue = AsyncMock()

        # Mock the parent issue creation
        callbacks.create_issue.return_value = 42

        # Mock the dispatch call
        dispatch_result = MagicMock()
        dispatch_result.status = "complete"
        dispatch_result.plan = {
            "score": 5000,
            "report": "report data",
            "scan_error": False,
        }
        callbacks.dispatch.return_value = dispatch_result

        phase = ScanningPhase()
        result = await phase.run(
            project_id="myrepo",
            threshold=7000,
            callbacks=callbacks,
        )

        # Verify dispatch was called with correct phase
        callbacks.dispatch.assert_called_once()
        call_kwargs = callbacks.dispatch.call_args[0]
        assert call_kwargs[1].phase == "code_quality_scan"

        # Verify result contains deserialized fields
        assert result["score"] == 5000
        assert result["report"] == "report data"
        assert result["scan_error"] is False

    @pytest.mark.asyncio
    async def test_posts_queued_comment_before_scan(self):
        """ScanningPhase posts a queued comment before dispatching scan."""
        callbacks = ScanningPhaseCallbacks()
        callbacks.dispatch = AsyncMock()
        callbacks.post_comment = AsyncMock()
        callbacks.create_issue = AsyncMock()

        callbacks.create_issue.return_value = 42

        dispatch_result = MagicMock()
        dispatch_result.status = "complete"
        dispatch_result.plan = {"score": 5000, "report": "", "scan_error": False}
        callbacks.dispatch.return_value = dispatch_result

        phase = ScanningPhase()
        await phase.run(
            project_id="myrepo",
            threshold=7000,
            callbacks=callbacks,
        )

        # Comment should be posted before dispatch
        post_comment_calls = callbacks.post_comment.call_args_list
        assert len(post_comment_calls) >= 1
        assert (
            post_comment_calls[0][0][2]
            == "⏳ queued — sentrux scan starting"
        )

    @pytest.mark.asyncio
    async def test_creates_parent_issue(self):
        """ScanningPhase creates a parent GitHub issue before dispatching scan."""
        callbacks = ScanningPhaseCallbacks()
        callbacks.dispatch = AsyncMock()
        callbacks.post_comment = AsyncMock()
        callbacks.create_issue = AsyncMock()

        callbacks.create_issue.return_value = 42

        dispatch_result = MagicMock()
        dispatch_result.status = "complete"
        dispatch_result.plan = {"score": 5000, "report": "", "scan_error": False}
        callbacks.dispatch.return_value = dispatch_result

        phase = ScanningPhase()
        await phase.run(
            project_id="myrepo",
            threshold=7000,
            callbacks=callbacks,
        )

        # Parent issue should be created
        callbacks.create_issue.assert_called_once()
        call_args = callbacks.create_issue.call_args[0]
        assert call_args[0].project_id == "myrepo"
        assert "devloop-code-quality" in call_args[0].labels

    @pytest.mark.asyncio
    async def test_returns_scan_error_from_plan(self):
        """ScanningPhase returns scan_error=True when plan indicates error."""
        callbacks = ScanningPhaseCallbacks()
        callbacks.dispatch = AsyncMock()
        callbacks.post_comment = AsyncMock()
        callbacks.create_issue = AsyncMock()

        callbacks.create_issue.return_value = 42

        dispatch_result = MagicMock()
        dispatch_result.status = "complete"
        dispatch_result.plan = {
            "score": 0,
            "report": "",
            "scan_error": True,
            "error_message": "no rules.toml found",
        }
        callbacks.dispatch.return_value = dispatch_result

        phase = ScanningPhase()
        result = await phase.run(
            project_id="myrepo",
            threshold=7000,
            callbacks=callbacks,
        )

        assert result["scan_error"] is True
        assert result["error_message"] == "no rules.toml found"
