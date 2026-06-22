"""ReviewPhase — review the PR and post findings.

Wraps the existing ``_review_phase`` activity call from ``DevLoopWorkflow``
as a standalone deep module with a small interface: ``run(inp, issue, callbacks)``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable, Coroutine, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

from .._constants import _RETRY, JOB_DISPATCH_QUEUE
from ..dev_loop_logic import render_review_findings_comment
from ..github import GithubNotificationInput
from ..phases.phase_ops import PhaseOps
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
_PostCommentCallback = Callable[[str, int, str], Coroutine[Any, Any, None]]


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
            callback=cb.post_comment,
        )
        result = await ops.dispatch_helper(
            inp.project_id,
            spec,
            issue_number=issue_no,
            poll_interval_seconds=inp.poll_interval_seconds,
            dispatch_callback=cb.dispatch_review,
            activity_name="dispatch_agent_job",
        )
        review = result.review or {}
        verdict = review.get("verdict") if review else None
        if verdict:
            await ops._phase_comment(
                inp.project_id,
                issue_no,
                f"🔎 Reviewed #{issue_no} — verdict: {verdict}.",
                callback=cb.post_comment,
            )
        else:
            await ops._phase_comment(
                inp.project_id,
                issue_no,
                f"🔎 Reviewed #{issue_no} — no changes needed.",
                callback=cb.post_comment,
            )

        # Post the reviewer's findings to the PR.
        await self._post_review_findings(
            inp.project_id,
            exec_result.get("pr_url", ""),
            review or {},
            result,
            cb,
        )

        return review or None

    async def _dispatch_review(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_number: int,
        poll_interval_seconds: float,
        cb: PhaseOps,
    ) -> AgentJobResult:
        """Dispatch the review agent job."""
        if cb.dispatch_review is not None:
            return await cb.dispatch_review(
                project_id, spec, issue_number, poll_interval_seconds
            )
        ops = PhaseOps()
        result = await ops.dispatch_helper(
            project_id,
            spec,
            issue_number,
            poll_interval_seconds,
            dispatch_callback=cb.dispatch_review,
            activity_name="dispatch_agent_job",
            task_queue=JOB_DISPATCH_QUEUE,
        )
        if result.status != "awaiting_human":
            await ops._phase_cleanup(result.job_name)
        return result

    async def _post_review_findings(
        self,
        project_id: str,
        pr_url: str,
        review: dict,
        result: AgentJobResult,
        cb: PhaseOps,
    ) -> None:
        """Post the reviewer's findings to the PR.

        ``create_pr`` opens PRs best-effort — a missing token scope or a
        pre-existing PR for the branch is logged, not raised, so the branch
        can land with ``pr_url == ""``. When that happens, fall back to a
        plain issue comment so findings still surface instead of dropping
        them or crashing the run.
        """
        if cb.post_review_findings is not None:
            await cb.post_review_findings(project_id, pr_url, review, result)
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

    async def _comment(
        self, project_id: str, issue_number: int, body: str, cb: PhaseOps
    ) -> None:
        """Post a GitHub Issue/PR comment."""
        if cb.post_comment is not None:
            await cb.post_comment(project_id, issue_number, body)
            return
        await workflow.execute_activity(
            "post_github_comment",
            GithubNotificationInput(
                issue_number=issue_number,
                project_id=project_id,
                body=body,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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
