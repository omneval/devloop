"""ReviewFixPass — one fix pass after a review's findings.

Wraps the existing ``_review_fix_pass`` activity call from ``DevLoopWorkflow``
as a standalone deep module with a small interface: ``run(inp, issue, exec_result, review, callbacks)``.

The reviewer's summary (which enumerates unmet acceptance criteria and
bugs) is handed to the fix agent exactly like a human PR comment would
be — the proven re-engagement path (omneval#70: the agent resolved
every finding of a human review in one such pass).

Returns True when the fix pass produced commits (a re-review is
worthwhile), False when it failed or changed nothing.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional

from .phase_ops import (
    PhaseOps,
    _DispatchFixCallback,
    _KpiBumpCallback,
    _PostCommentCallback,
)
from ..shared import TaskSpec


class ReviewFixPass:
    """One fix pass after a review's findings.

    Stateless — all context flows through ``run`` parameters.
    """

    async def run(
        self,
        inp: Any,  # DevLoopInput
        issue: dict,
        exec_result: dict,
        review: dict,
        callbacks: Optional[PhaseOps] = None,
    ) -> bool:
        """Run one review fix pass.

        Parameters
        ----------
        inp : DevLoopInput
            Workflow input (must have ``project_id``, ``poll_interval_seconds``).
        issue : dict
            Plan issue dict (must have ``id``).
        exec_result : dict
            Execute result dict (must have ``pr_url``, ``branch``).
        review : dict
            Review payload (may have ``summary``, ``inline_comments``).
        callbacks : PhaseOps, optional
            Injected callbacks for testing.

        Returns
        -------
        bool
            True when the fix pass produced commits, False otherwise.
        """
        cb = callbacks or PhaseOps.default()
        ops = PhaseOps()
        issue_no = ops.as_int(issue.get("id"))
        pr_url = exec_result.get("pr_url", "")
        pr_number = ops.pr_number_from_url(pr_url)
        findings = review.get("summary", "")
        inline = review.get("inline_comments") or []
        if inline:
            findings += "\n\nInline comments:\n" + "\n".join(
                f"- {c.get('file', '')}:{c.get('line', 0)} — {c.get('body', '')}"
                for c in inline
            )
        if not findings.strip():
            return False

        await ops._phase_comment(
            inp.project_id,
            issue_no,
            "⏳ queued — agent is addressing automated review findings",
            callback=cb.post_comment,
        )
        spec = TaskSpec(
            phase="pr_comment",
            project_id=inp.project_id,
            issue_number=issue_no,
            branch=exec_result.get("branch", ""),
            extra={
                "comment_body": findings,
                "source": "review",
                "author": "the automated reviewer",
                "pr_number": pr_number,
            },
        )
        result = await ops.dispatch_helper(
            inp.project_id,
            spec,
            issue_number=issue_no,
            poll_interval_seconds=inp.poll_interval_seconds,
            dispatch_callback=cb.dispatch_fix,  # ty: ignore[invalid-argument-type]
        )
        if not _has_commits(result):
            return False
        await ops._phase_comment(
            inp.project_id,
            issue_no,
            f"🔧 Fix pass pushed {_commits_count(result)} commit(s) addressing review findings.",
            callback=cb.post_comment,
        )
        return True

    async def _dispatch_fix(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_number: int,
        poll_interval_seconds: float,
        cb: PhaseOps,
    ) -> int:
        """Dispatch the fix agent job via the PhaseOps protocol.

        Serialises *spec* to a dict because the ``PhaseOps.dispatch_fix``
        callback signature expects `(str, int, dict, float) -> int` (#188).
        """
        if cb.dispatch_fix is not None:
            return await cb.dispatch_fix(
                project_id, issue_number, asdict(spec), poll_interval_seconds
            )
        # Default path: return 0 commits so the caller sees "no changes".
        return 0

    async def _post_comment(
        self,
        project_id: str,
        issue_number: int,
        body: str,
        cb: Optional[PhaseOps] = None,
    ) -> None:
        """Post a GitHub Issue/PR comment."""
        if cb and cb._phase_comment is not None:
            await cb._phase_comment(
                project_id,
                issue_number,
                body,
            )
            return

    async def _kpi_bump(
        self, name: str, value: int, cb: Optional[PhaseOps] = None
    ) -> None:
        """Record a KPI metric."""
        if cb and cb.kpi_bump is not None:
            await cb.kpi_bump(name, value)


def _has_commits(result: Any) -> bool:
    """Check if a dispatch result has non-zero commits.

    ``PhaseOps.dispatch_fix`` returns an ``int`` directly (#188).
    """
    if isinstance(result, int):
        return result > 0
    return getattr(result, "commits", 0) > 0 or (
        isinstance(result, dict) and result.get("commits", 0) > 0
    )


def _commits_count(result: Any) -> int:
    """Get the commit count from a dispatch result.

    ``PhaseOps.dispatch_fix`` returns an ``int`` directly (#188).
    """
    if isinstance(result, int):
        return result
    if isinstance(result, dict):
        return result.get("commits", 0)
    return getattr(result, "commits", 0)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class ReviewFixPassCallbacks(PhaseOps):
    """Backward-compatible shim that delegates to a ``PhaseOps`` instance.

    This class exists only for callers that still construct
    ``ReviewFixPassCallbacks(dispatch_fix=..., ...)`` directly.  On
    construction it creates a ``PhaseOps`` that carries the same fields,
    so all downstream code uses the unified protocol.

    Subclassing ``PhaseOps`` so that consumers expecting a ``PhaseOps``
    instance still work.
    """

    def __init__(
        self,
        dispatch_fix: Optional[_DispatchFixCallback] = None,
        post_comment: Optional[_PostCommentCallback] = None,
        kpi_bump: Optional[_KpiBumpCallback] = None,
        **kwargs: Any,
    ) -> None:
        PhaseOps.__init__(
            self,
            dispatch_fix=dispatch_fix,
            comment=post_comment,
            kpi_bump=kpi_bump,
            **kwargs,
        )

    @classmethod
    def default(cls) -> "ReviewFixPassCallbacks":
        return cls()

    @property
    def phaseops(self) -> PhaseOps:
        return self


# Re-export for convenience.
PhaseOpsCallbacks = PhaseOps  # noqa: F401
ReviewFixPassCallbacks = ReviewFixPassCallbacks  # noqa: F401
