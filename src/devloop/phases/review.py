"""ReviewPhase — review the PR and post findings.

Wraps the existing ``_review_phase`` activity call from ``DevLoopWorkflow``
as a standalone deep module with a small interface: ``run(inp, issue, callbacks)``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable, Coroutine, Optional

from temporalio import workflow

from .._constants import _RETRY
from ..dev_loop_logic import render_review_findings_comment
from ..phases.phase_ops import PhaseOps, _PostCommentCallback
from ..shared import (
    AgentJobResult,
    InlineComment,
    PostCommentsInput,
    TaskSpec,
)


# Type aliases for injectable callbacks.
_DispatchReviewCallback = Callable[
    [str, TaskSpec, int, float], Coroutine[Any, Any, AgentJobResult]
]
_PostReviewFindingsCallback = Callable[
    [str, str, dict, AgentJobResult], Coroutine[Any, Any, None]
]


class ReviewPhase:
    """Review the PR and post findings.

    Stateless — all context flows through ``run`` parameters.
    """

    async def run(
        self,
        inp: Any,  # DevLoopInput
        issue: dict,
        exec_result: dict,
        callbacks: Optional[PhaseOps] = None,
    ) -> dict | None:
        """Review the PR and return the review payload.

        Parameters
        ----------
        inp : DevLoopInput
            Workflow input (must have ``project_id``, ``poll_interval_seconds``).
        issue : dict
            Plan issue dict (must have ``id``).
        exec_result : dict
            Execute result dict (must have ``branch``, ``pr_url``).
        callbacks : PhaseOps, optional
            Injected callbacks for testing.

        Returns
        -------
        dict | None
            A review dict with a ``verdict`` key, or ``None`` when
            the review job produced nothing parseable.
        """
        cb = callbacks or PhaseOps.default()
        ops = PhaseOps()
        issue_no = ops.as_int(issue.get("id"))

        # Use review_ops sub-protocol with fallback to top-level PhaseOps fields.
        review_ops = cb.review_ops
        _comment_cb = review_ops.comment or cb.post_comment

        spec = TaskSpec(
            phase="review",
            project_id=inp.project_id,
            issue_number=issue_no,
            branch=exec_result["branch"],
        )
        await ops._phase_comment(
            inp.project_id,
            issue_no,
            "⏳ queued — agent is reviewing this issue",
            callback=_comment_cb,
        )
        _dispatch_cb = review_ops.dispatch_review or cb.dispatch_review
        result = await ops.dispatch_helper(
            inp.project_id,
            spec,
            issue_number=issue_no,
            poll_interval_seconds=inp.poll_interval_seconds,
            dispatch_callback=_dispatch_cb,
            activity_name="dispatch_agent_job",
        )
        review = result.review or {}
        verdict = review.get("verdict") if review else None
        if verdict:
            await ops._phase_comment(
                inp.project_id,
                issue_no,
                f"🔎 Reviewed #{issue_no} — verdict: {verdict}.",
                callback=_comment_cb,
            )
        else:
            await ops._phase_comment(
                inp.project_id,
                issue_no,
                f"🔎 Reviewed #{issue_no} — no changes needed.",
                callback=_comment_cb,
            )

        # Post the reviewer's findings to the PR.
        _post_review_cb = review_ops.post_review_findings or cb.post_review_findings
        await self._post_review_findings(
            inp.project_id,
            exec_result.get("pr_url", ""),
            review or {},
            result,
            cb,
            _post_review_cb,
        )

        return review or None

    async def _post_review_findings(
        self,
        project_id: str,
        pr_url: str,
        review: dict,
        result: AgentJobResult,
        cb: PhaseOps,
        post_review_findings_callback: Optional[
            Callable[[str, str, dict, AgentJobResult], Coroutine[Any, Any, None]]
        ] = None,
    ) -> None:
        """Post the reviewer's findings to the PR.

        ``create_pr`` opens PRs best-effort — a missing token scope or a
        pre-existing PR for the branch is logged, not raised, so the branch
        can land with ``pr_url == ""``. When that happens, fall back to a
        plain issue comment so findings still surface instead of dropping
        them or crashing the run.
        """
        _cb = post_review_findings_callback or cb.post_review_findings
        if _cb is not None:
            await _cb(project_id, pr_url, review, result)
            return
        # Default: real Temporal activity path.
        ops = PhaseOps()
        summary = review.get("summary", "")
        inline = [
            InlineComment(
                file=c.get("file", ""),
                line=ops.as_int(c.get("line")),
                body=c.get("body", ""),
            )
            for c in (review.get("inline_comments") or [])
        ]
        if not summary and not inline:
            return

        pr_number = ops.pr_number_from_url(pr_url)
        if not pr_number:
            await ops._phase_comment(
                project_id,
                result.issue_number,
                render_review_findings_comment(summary, inline),
                callback=cb.post_comment,
            )
            return
        await workflow.execute_activity(
            "post_pr_comments",
            PostCommentsInput(project_id, pr_number, summary, inline),
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_RETRY,
        )


class ReviewPhaseCallbacks(PhaseOps):
    """Backward-compatible shim that delegates to a ``PhaseOps`` instance.

    This class exists only for callers that still construct
    ``ReviewPhaseCallbacks(dispatch_review=..., ...)`` directly.  On
    construction it creates a ``PhaseOps`` that carries the same fields,
    so all downstream code uses the unified protocol.

    Subclassing ``PhaseOps`` so that consumers expecting a ``PhaseOps``
    instance still work.
    """

    def __init__(
        self,
        dispatch_review: Optional[_DispatchReviewCallback] = None,
        post_review_findings: Optional[_PostReviewFindingsCallback] = None,
        post_comment: Optional[_PostCommentCallback] = None,
        **kwargs: Any,
    ) -> None:
        PhaseOps.__init__(
            self,
            dispatch_review=dispatch_review,
            post_review_findings=post_review_findings,
            comment=post_comment,
            **kwargs,
        )

    @classmethod
    def default(cls) -> "ReviewPhaseCallbacks":
        return cls()

    @property
    def phaseops(self) -> PhaseOps:
        return self
