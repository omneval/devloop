"""Unit tests for devloop.phases.notifier — Notifier standalone module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from devloop.phases.notifier import Notifier, PhaseOps


class TestNotifier:
    """Notifier — request a reviewer and post a notification."""

    @pytest.mark.asyncio
    async def test_success_tagged_reviewer(self) -> None:
        """Notifier posts comment when reviewer is successfully tagged."""
        notifier = Notifier()
        callbacks = PhaseOps(
            request_reviewer=AsyncMock(
                return_value=MagicMock(requested=True, reason=None)
            ),
            post_comment=AsyncMock(),
        )
        inp = MagicMock(project_id="proj")
        issue = {"id": "42"}
        exec_result = {
            "pr_url": "https://github.com/user/repo/pull/42",
            "exhausted": False,
        }

        await notifier.run(
            inp=inp,
            issue=issue,
            exec_result=exec_result,
            callbacks=callbacks,
        )

        call_body = callbacks.post_comment.call_args[0][2]
        assert "Reviewer has been tagged" in call_body

    @pytest.mark.asyncio
    async def test_no_reviewer_configured(self) -> None:
        """Notifier posts 'no reviewer requested' when reviewer is skipped."""
        notifier = Notifier()
        callbacks = PhaseOps(
            request_reviewer=AsyncMock(
                return_value=MagicMock(requested=False, reason="no config")
            ),
            post_comment=AsyncMock(),
        )
        inp = MagicMock(project_id="proj")
        issue = {"id": "42"}
        exec_result = {
            "pr_url": "https://github.com/user/repo/pull/42",
            "exhausted": False,
        }

        await notifier.run(
            inp=inp,
            issue=issue,
            exec_result=exec_result,
            callbacks=callbacks,
        )

        call_body = callbacks.post_comment.call_args[0][2]
        assert "No reviewer was requested" in call_body

    @pytest.mark.asyncio
    async def test_exhausted_uses_warning_comment(self) -> None:
        """Notifier includes CI warning when exhausted=True."""
        notifier = Notifier()
        callbacks = PhaseOps(
            request_reviewer=AsyncMock(
                return_value=MagicMock(requested=True, reason=None)
            ),
            post_comment=AsyncMock(),
        )
        inp = MagicMock(project_id="proj")
        issue = {"id": "42"}
        exec_result = {
            "pr_url": "https://github.com/user/repo/pull/42",
            "exhausted": True,
        }

        await notifier.run(
            inp=inp,
            issue=issue,
            exec_result=exec_result,
            callbacks=callbacks,
        )

        call_body = callbacks.post_comment.call_args[0][2]
        assert "CI is still failing" in call_body
