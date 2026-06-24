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

from typing import Any, Optional

from .phase_ops import (
    PhaseOps,
    _DispatchFixCallback,
)
from ..phases._utils import callback_or_ops
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

        # Use ci_ops sub-protocol with fallback to top-level PhaseOps fields.
        ci_ops = cb.ci_ops
        _comment_cb = callback_or_ops(ci_ops.comment, cb.post_comment)

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
            callback=_comment_cb,
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
        # Use the _dispatch_fix helper which calls the dispatch_fix callback
        # directly (cb.dispatch_fix returns an int, not AgentJobResult).
        _dispatch_cb = callback_or_ops(ci_ops.dispatch_fix, cb.dispatch_fix)
        commits = await self._dispatch_fix(
            inp.project_id,
            spec,
            issue_no,
            inp.poll_interval_seconds,
            cb,
            _dispatch_cb,
        )
        if not commits:
            return False
        await ops._phase_comment(
            inp.project_id,
            issue_no,
            f"🔧 Fix pass pushed {commits} commit(s) addressing review findings.",
            callback=_comment_cb,
        )
        return True

    async def _dispatch_fix(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_number: int,
        poll_interval_seconds: float,
        cb: PhaseOps,
        dispatch_callback: Optional[_DispatchFixCallback] = None,
    ) -> int:
        """Dispatch the fix agent job via the PhaseOps protocol."""
        _cb = dispatch_callback or cb.dispatch_fix
        if _cb is not None:
            return await _cb(project_id, spec, issue_number, poll_interval_seconds)
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
