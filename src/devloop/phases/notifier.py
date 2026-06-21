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

from typing import Any, Callable, Coroutine, Optional

from devloop.github import ReviewerRequestResult

from .phase_ops import PhaseOps

# Re-use types from the unified protocol
_PostCommentCallback = Callable[[str, int, str], Coroutine[Any, Any, None]]
_RequestReviewerCallback = Callable[
    [str, Optional[int]], Coroutine[Any, Any, ReviewerRequestResult]
]


class Notifier:
    """Request a GitHub PR reviewer and post a notification comment.

    Stateless — all context flows through ``run`` parameters.
    """

    async def run(
        self,
        inp: Any,  # DevLoopInput
        issue: dict,
        exec_result: dict,
        callbacks: Optional[PhaseOps] = None,
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
        callbacks : PhaseOps, optional
            Injected callbacks for testing.
        """
        cb = callbacks or PhaseOps.default()
        ops = PhaseOps()
        issue_no = ops.as_int(issue.get("id"))
        pr_url = exec_result.get("pr_url", "")
        pr_number = ops.pr_number_from_url(pr_url)

        reviewer_result = await ops._phase_request_reviewer(
            inp.project_id,
            pr_number,
            callback=cb._phase_request_reviewer_callback,
        )
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
        await ops._phase_comment(
            inp.project_id,
            issue_no,
            f"👀 Ready for review — PR: {pr_url}. {reviewer_note}{note}",
            callback=cb.post_comment,
        )

    async def _request_reviewer(
        self, project_id: str, pr_number: Optional[int], cb: PhaseOps
    ) -> Any:
        """Request a GitHub PR reviewer (or use injected callback)."""
        if cb._phase_request_reviewer_callback is not None:
            return await cb._phase_request_reviewer_callback(  # ty: ignore[missing-argument]
                project_id,  # ty: ignore[invalid-argument-type]
                pr_number,
            )
        return None

    async def _post_comment(
        self, project_id: str, issue_number: int, body: str, cb: PhaseOps
    ) -> None:
        """Post a GitHub Issue/PR comment."""
        if cb._phase_comment is not None:
            await cb._phase_comment(
                project_id,
                issue_number,
                body,
            )
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


class NotifierCallbacks(PhaseOps):
    """Backward-compatible shim that delegates to a ``PhaseOps`` instance.

    This class exists only for callers that still construct
    ``NotifierCallbacks(request_reviewer=..., ...)`` directly.  On
    construction it creates a ``PhaseOps`` that carries the same fields,
    so all downstream code uses the unified protocol.

    Subclassing ``PhaseOps`` so that consumers expecting a ``PhaseOps``
    instance (e.g. code that reads ``cb.post_comment``) still work.
    """

    def __init__(
        self,
        request_reviewer: Optional[_RequestReviewerCallback] = None,
        post_comment: Optional[_PostCommentCallback] = None,
        **kwargs: Any,
    ) -> None:
        PhaseOps.__init__(
            self,
            request_reviewer=request_reviewer,
            comment=post_comment,
            **kwargs,
        )

    @classmethod
    def default(cls) -> "NotifierCallbacks":
        return cls()

    @property
    def phaseops(self) -> PhaseOps:
        return self


# Re-export for convenience.
PhaseOpsCallbacks = PhaseOps  # noqa: F401
NotifierCallbacks = NotifierCallbacks  # noqa: F401
