"""Notifier — request a reviewer and post a notification comment.

Wraps the existing ``_notify_reviewer`` activity call from
``DevLoopWorkflow`` as a standalone deep module with a small
interface: ``run(inp, issue, exec_result, callbacks)``.

Reads ``pr_reviewer`` from the project's ``ProjectConfig`` (via the
``request_github_reviewer`` activity). The notification only claims a
reviewer was tagged when the request actually succeeded — when it was
skipped (no reviewer configured, no PR to request on) or failed (a
GitHub API error), the comment says so honestly instead (issue #88);
a confidently-wrong "tagged" claim would mislead the human who's
supposed to act on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Optional


# Type aliases for injectable callbacks.
_RequestReviewerCallback = Callable[[str, Optional[int]], Coroutine[Any, Any, Any]]
_PostCommentCallback = Callable[[str, int, str], Coroutine[Any, Any, None]]


@dataclass
class _Callbacks:
    """Callback set for Notifier.run().

    When all fields are ``None``, the default Temporal activity paths are used.
    """

    request_reviewer: Optional[_RequestReviewerCallback] = None
    post_comment: Optional[_PostCommentCallback] = None

    @classmethod
    def default(cls) -> "_Callbacks":
        """Return a callbacks instance that delegates to Temporal activities."""
        return cls()


class Notifier:
    """Request a GitHub PR reviewer and post a notification comment.

    Stateless — all context flows through ``run`` parameters.
    """

    async def run(
        self,
        inp: Any,  # DevLoopInput
        issue: dict,
        exec_result: dict,
        callbacks: Optional[_Callbacks] = None,
    ) -> None:
        """Notify the reviewer.

        Parameters
        ----------
        inp : DevLoopInput
            Workflow input (must have ``project_id``).
        issue : dict
            Plan issue dict (must have ``id``).
        exec_result : dict
            Execute result dict (must have ``pr_url``).
        callbacks : _Callbacks, optional
            Injected callbacks for testing.
        """
        cb = callbacks or _Callbacks.default()
        issue_no = _as_int(issue.get("id"))
        pr_url = exec_result.get("pr_url", "")
        pr_number = _pr_number_from_url(pr_url)

        reviewer_result = await self._request_reviewer(inp.project_id, pr_number, cb)
        if reviewer_result.requested:
            reviewer_note = "Reviewer has been tagged."
        elif reviewer_result.reason:
            reviewer_note = f"No reviewer was requested ({reviewer_result.reason})."
        else:
            reviewer_note = "No reviewer was requested."

        note = (
            " ⚠️ CI is still failing after exhausting the CI fix attempts —"
            " please take a look."
            if exec_result.get("exhausted")
            else ""
        )
        await self._post_comment(
            inp.project_id,
            issue_no,
            f"👀 Ready for review — PR: {pr_url}. {reviewer_note}{note}",
            cb,
        )

    async def _request_reviewer(
        self, project_id: str, pr_number: Optional[int], cb: _Callbacks
    ) -> Any:
        """Request a GitHub PR reviewer (or use injected callback)."""
        if cb.request_reviewer is not None:
            return await cb.request_reviewer(project_id, pr_number)
        return None

    async def _post_comment(
        self, project_id: str, issue_number: int, body: str, cb: _Callbacks
    ) -> None:
        """Post a GitHub Issue/PR comment."""
        if cb.post_comment is not None:
            await cb.post_comment(project_id, issue_number, body)
            return


def _pr_number_from_url(pr_url: str) -> Optional[int]:
    """Extract the PR number from a GitHub PR URL."""
    if not pr_url:
        return None
    parts = pr_url.rstrip("/").split("/")
    try:
        return int(parts[-1])
    except (ValueError, IndexError):
        return None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# Re-export for convenience.
NotifierCallbacks = _Callbacks
