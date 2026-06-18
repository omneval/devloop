"""Shared workflow helpers (issue #78).

``DevLoopWorkflow`` and ``PRCommentWorkflow`` both need to post GitHub Issue/PR
comments, dispatch Agent Execution Jobs, and request a GitHub PR reviewer.
Rather than duplicate that logic, it lives here as a mixin (``_WorkflowCommon``)
both workflow classes inherit from — methods are plain ``async def`` calls into
``workflow.execute_activity`` so they stay sandbox-safe and behave identically
regardless of which workflow calls them.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from ._constants import _ACTIVITY_TIMEOUT, _GITHUB_COMMENT_TIMEOUT, _RETRY
from .shared import (
    AgentJobResult,
    DispatchInput,
    GithubNotificationInput,
    JOB_DISPATCH_QUEUE,
    JobStatus,
    RequestReviewerInput,
    ReviewerRequestResult,
    TaskSpec,
    WorkflowKpiInput,
)

_CLEANUP_RETRY = RetryPolicy(maximum_attempts=1)


class _WorkflowCommon:
    """Mixin of activity-calling helpers shared across Dev Loop workflows.

    Any workflow mixing this in must expose a ``project_id``-bearing input
    object via the ``inp`` parameter on each call — the helpers themselves
    hold no state (Temporal workflow instances are re-hydrated from history,
    so state must live in the workflow's own ``__init__``/run-local scope).
    """

    # ---- Workflow KPI counters (issue #122) ------------------------------- #
    def _kpi_bump(self, key: str, n: int = 1) -> None:
        """Increment a per-issue KPI counter (lazily initialised — the mixin
        has no __init__). Counters are plain workflow state, so they replay
        deterministically."""
        counters = getattr(self, "_kpi_counters", None)
        if counters is None:
            counters = {}
            self._kpi_counters = counters
        counters[key] = counters.get(key, 0) + n

    def _kpi_take(self) -> dict:
        """Return and reset the accumulated counters (one issue's worth)."""
        counters = getattr(self, "_kpi_counters", None) or {}
        self._kpi_counters = {}
        return counters

    async def _emit_kpis(self, inp: WorkflowKpiInput) -> None:
        """Fire the emit_workflow_kpis activity — strictly best-effort: a
        telemetry hiccup must never fail or retry-storm the workflow."""
        try:
            await workflow.execute_activity(
                "emit_workflow_kpis",
                inp,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except Exception:  # noqa: BLE001
            workflow.logger.warning("emit_workflow_kpis failed (ignored)")

    # ---- ConfigMap cleanup (issue #99) ----------------------------------- #
    async def _cleanup(self, job_name: str) -> None:
        """Delete the output ConfigMap for a completed job — fire-and-forget.

        Failures are swallowed; a leaked ConfigMap is preferable to a stalled
        workflow. The K8s Job itself is cleaned up by ttlSecondsAfterFinished.
        """
        if not job_name:
            return
        try:
            await workflow.execute_activity(
                "cleanup_configmap",
                job_name,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_CLEANUP_RETRY,
            )
        except Exception:
            workflow.logger.warning("cleanup_configmap failed for %s", job_name)

    # ---- GitHub Issue/PR comment helper ---------------------------------- #
    async def _comment(self, project_id: str, issue_number: int, body: str) -> None:
        """Post a comment on the given GitHub Issue/PR via devloop-bot."""
        await workflow.execute_activity(
            "post_github_comment",
            GithubNotificationInput(
                issue_number=issue_number,
                project_id=project_id,
                body=body,
            ),
            start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
            retry_policy=_RETRY,
        )

    # ---- Agent Execution Job dispatch ------------------------------------ #
    async def _dispatch(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_number: int = 0,
        poll_interval_seconds: float = 5.0,
    ) -> AgentJobResult:
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
        if result.status != JobStatus.AWAITING_HUMAN.value:
            await self._cleanup(result.job_name)
        return result

    # ---- Reviewer request (#74) ------------------------------------------- #
    async def _request_reviewer(
        self, project_id: str, pr_number: int
    ) -> ReviewerRequestResult:
        """Request a GitHub PR reviewer via the project's configured reviewer.

        The actual reviewer login is resolved by the activity from the
        project registry — workflows pass an empty string and the activity
        fills it in, keeping the I/O (and the registry lookup) out of the
        sandbox.

        Returns the activity's ``ReviewerRequestResult`` (requested or
        skipped/failed-with-reason) so callers like ``_notify_reviewer`` can
        report honestly on whether a reviewer was actually tagged (issue #88)
        rather than assuming success.
        """
        return await workflow.execute_activity(
            "request_github_reviewer",
            RequestReviewerInput(
                project_id=project_id,
                pr_number=pr_number,
                reviewer="",
            ),
            result_type=ReviewerRequestResult,
            start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
            retry_policy=_RETRY,
        )
