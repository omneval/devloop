"""ReviewPhase — review the PR and post findings.

Wraps the existing ``_review_phase`` activity call from ``DevLoopWorkflow``
as a standalone deep module with a small interface: ``run(inp, issue, callbacks)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Coroutine, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

from .._constants import _ACTIVITY_TIMEOUT, _RETRY
from ..shared import (
    AgentJobResult,
    DispatchInput,
    GithubNotificationInput,
    InlineComment,
    JOB_DISPATCH_QUEUE,
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


@dataclass
class _Callbacks:
    """Callback set for ReviewPhase.run().

    When all fields are ``None``, the default Temporal activity paths are used.
    """

    dispatch_review: Optional[_DispatchReviewCallback] = None
    post_review_findings: Optional[_PostReviewFindingsCallback] = None
    post_comment: Optional[_PostCommentCallback] = None

    @classmethod
    def default(cls) -> "_Callbacks":
        """Return a callbacks instance that delegates to Temporal activities."""
        return cls()


class ReviewPhase:
    """Review the PR and post findings.

    Stateless — all context flows through ``run`` parameters.
    """

    async def run(
        self,
        inp: Any,  # DevLoopInput
        issue: dict,
        exec_result: dict,
        callbacks: Optional[_Callbacks] = None,
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
        callbacks : _Callbacks, optional
            Injected callbacks for testing.

        Returns
        -------
        dict | None
            A review dict with a ``verdict`` key, or ``None`` when
            the review job produced nothing parseable.
        """
        cb = callbacks or _Callbacks.default()
        issue_no = _as_int(issue.get("id"))

        spec = TaskSpec(
            phase="review",
            project_id=inp.project_id,
            issue_number=issue_no,
            branch=exec_result["branch"],
        )
        await self._comment(
            inp.project_id,
            issue_no,
            "⏳ queued — agent is reviewing this issue",
            cb,
        )
        result = await self._dispatch_review(
            inp.project_id,
            spec,
            issue_number=issue_no,
            poll_interval_seconds=inp.poll_interval_seconds,
            cb=cb,
        )
        review = result.review or {}
        verdict = review.get("verdict") if review else None
        if verdict:
            await self._comment(
                inp.project_id,
                issue_no,
                f"🔎 Reviewed #{issue_no} — verdict: {verdict}.",
                cb,
            )
        else:
            await self._comment(
                inp.project_id,
                issue_no,
                f"🔎 Reviewed #{issue_no} — no changes needed.",
                cb,
            )

        # Post the reviewer's findings to the PR.
        try:
            await self._post_review_findings(
                inp.project_id,
                exec_result.get("pr_url", ""),
                review or {},
                result,
                cb,
            )
        except RuntimeError:
            # _post_review_findings raises when pr_url is unparseable —
            # re-raise so the caller can decide how to handle.
            raise

        return review or None

    async def _dispatch_review(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_number: int,
        poll_interval_seconds: float,
        cb: _Callbacks,
    ) -> AgentJobResult:
        """Dispatch the review agent job."""
        if cb.dispatch_review is not None:
            return await cb.dispatch_review(
                project_id, spec, issue_number, poll_interval_seconds
            )
        result = await workflow.execute_activity(
            "dispatch_agent_job",
            DispatchInput(
                project_id,
                issue_number,
                spec,
                poll_interval_seconds=poll_interval_seconds,
            ),
            result_type=AgentJobResult,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
            task_queue=JOB_DISPATCH_QUEUE,
        )
        if result.status != "awaiting_human":
            await self._cleanup(result.job_name)
        return result

    async def _post_review_findings(
        self,
        project_id: str,
        pr_url: str,
        review: dict,
        result: AgentJobResult,
        cb: _Callbacks,
    ) -> None:
        """Post the reviewer's findings to the PR."""
        if cb.post_review_findings is not None:
            await cb.post_review_findings(project_id, pr_url, review, result)
            return
        # Default: real Temporal activity path.
        summary = review.get("summary", "")
        inline = [
            InlineComment(
                file=c.get("file", ""),
                line=_as_int(c.get("line")),
                body=c.get("body", ""),
            )
            for c in (review.get("inline_comments") or [])
        ]
        if not summary and not inline:
            return
        from .. import dev_loop_logic as logic

        pr_number = logic.pr_number_from_url(pr_url)
        if not pr_number:
            raise RuntimeError(
                f"cannot post review findings: pr_url '{pr_url}' "
                f"for project {project_id} is unparseable or missing"
            )
        await workflow.execute_activity(
            "post_pr_comments",
            PostCommentsInput(project_id, pr_number, summary, inline),
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

    async def _comment(
        self, project_id: str, issue_number: int, body: str, cb: _Callbacks
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

    async def _cleanup(self, job_name: str) -> None:
        """Delete the output ConfigMap for a completed job."""
        if not job_name:
            return
        try:
            await workflow.execute_activity(
                "cleanup_configmap",
                job_name,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except Exception:  # noqa: BLE001
            workflow.logger.warning("cleanup_configmap failed for %s", job_name)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# Re-export for convenience.
ReviewPhaseCallbacks = _Callbacks
