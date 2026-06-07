"""Shared workflow helpers (issue #78).

``DevLoopWorkflow`` and ``PRCommentWorkflow`` both need to post GitHub Issue/PR
comments, dispatch Agent Execution Jobs, run the Phase.CI_FIX retry loop, and
request a GitHub PR reviewer. Rather than duplicate that logic, it lives here
as a mixin (``_WorkflowCommon``) both workflow classes inherit from — methods
are plain ``async def`` calls into ``workflow.execute_activity`` so they stay
sandbox-safe and behave identically regardless of which workflow calls them.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from . import dev_loop_logic as logic
from .shared import (
    AgentJobResult,
    CIChecksResult,
    DispatchInput,
    GithubNotificationInput,
    JOB_DISPATCH_QUEUE,
    JobStatus,
    Phase,
    PollCIChecksInput,
    RequestReviewerInput,
    TaskSpec,
)

_RETRY = RetryPolicy(maximum_attempts=3)
_ACTIVITY_TIMEOUT = timedelta(hours=2)
_GITHUB_COMMENT_TIMEOUT = timedelta(seconds=60)


class _WorkflowCommon:
    """Mixin of activity-calling helpers shared across Dev Loop workflows.

    Any workflow mixing this in must expose a ``project_id``-bearing input
    object via the ``inp`` parameter on each call — the helpers themselves
    hold no state (Temporal workflow instances are re-hydrated from history,
    so state must live in the workflow's own ``__init__``/run-local scope).
    """

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
        return await workflow.execute_activity(
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

    # ---- Phase.CI_FIX loop (#76) ------------------------------------------ #
    async def _ci_fix_loop(
        self,
        project_id: str,
        issue_no: int,
        exec_result: dict,
        ci_fix_max_iterations: int,
        poll_interval_seconds: float = 5.0,
    ) -> bool:
        """Retry CI fixes up to ``ci_fix_max_iterations`` times or until green.

        Each iteration polls the PR's CI checks; if they all pass, the loop
        exits early (``exhausted=False``). Otherwise it dispatches a
        ``Phase.CI_FIX`` Agent Execution Job — preceded by a "⏳ queued"
        comment — with the current failing check details in
        ``TaskSpec.extra["ci_check_failures"]``, then posts a result comment.

        Returns ``True`` when every iteration is spent without CI going green
        (``exhausted``), so the caller can carry a "CI still failing" note to
        the human reviewer.
        """
        pr_number = logic.pr_number_from_url(exec_result.get("pr_url", ""))
        if pr_number <= 0:
            return False

        max_iters = ci_fix_max_iterations
        for attempt in range(1, max_iters + 1):
            checks = await workflow.execute_activity(
                "poll_ci_checks",
                PollCIChecksInput(project_id=project_id, pr_number=pr_number),
                result_type=CIChecksResult,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=_RETRY,
            )
            if checks.all_passed:
                return False

            failures = [
                {
                    "name": f.name,
                    "conclusion": f.conclusion,
                    "details_url": f.details_url,
                    "summary": f.summary,
                }
                for f in (checks.failures or [])
            ]
            spec = TaskSpec(
                phase=Phase.CI_FIX.value,
                project_id=project_id,
                issue_number=issue_no,
                branch=exec_result.get("branch", ""),
                extra={"ci_check_failures": failures},
            )
            await self._comment(
                project_id,
                issue_no,
                f"⏳ queued — CI fix attempt {attempt}/{max_iters}",
            )
            result = await self._dispatch(
                project_id,
                spec,
                issue_number=issue_no,
                poll_interval_seconds=poll_interval_seconds,
            )
            if result.status == JobStatus.COMPLETE.value:
                await self._comment(
                    project_id,
                    issue_no,
                    f"🔧 CI fix attempt {attempt}/{max_iters} — "
                    f"pushed {result.commits} commit(s)",
                )
            else:
                await self._comment(
                    project_id,
                    issue_no,
                    f"❌ CI fix attempt {attempt}/{max_iters} failed",
                )

        return True

    # ---- Reviewer request (#74) ------------------------------------------- #
    async def _request_reviewer(self, project_id: str, pr_number: int) -> None:
        """Request a GitHub PR reviewer via the project's configured reviewer.

        The actual reviewer login is resolved by the activity from the
        project registry — workflows pass an empty string and the activity
        fills it in, keeping the I/O (and the registry lookup) out of the
        sandbox.
        """
        await workflow.execute_activity(
            "request_github_reviewer",
            RequestReviewerInput(
                project_id=project_id,
                pr_number=pr_number,
                reviewer="",
            ),
            start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
            retry_policy=_RETRY,
        )
