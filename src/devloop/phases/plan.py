"""PlanPhase — plan the next round of issues.

Wraps the existing ``_plan_phase`` activity call from ``DevLoopWorkflow``
as a standalone deep module with a small interface: ``run(inp, rnd, callbacks)``.

Two paths:
* **Webhook-triggered** (``triggering_issue > 0``): lightweight ``plan_issue``
  activity — one GitHub API call to confirm the issue is open and still
  labelled, then a string-format for the branch slug (issue #120).
* **Backlog** (``triggering_issue == 0``): full Plan Agent Execution Job
  dispatch for backlog reasoning.

After plan resolution, ``_drop_issues_in_review`` filters out issues that
already have an open agent PR so the workflow doesn't re-surface them.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable, Coroutine, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

from .._constants import _ACTIVITY_TIMEOUT, _RETRY
from ..shared import (
    AgentJobResult,
    DispatchInput,
    JOB_DISPATCH_QUEUE,
    PlanIssueInput,
    TaskSpec,
)


# Re-use types from the unified protocol
from .io_ops import (
    _DispatchPlanCallback,
    _DropInReviewCallback,
    _KpiBumpCallback,
    _PostCommentCallback,
)
from .phase_ops import PhaseOps


class PlanPhase:
    """Plan the next round of issues.

    Stateless — all context flows through ``run`` parameters.
    """

    async def run(
        self,
        inp: Any,  # DevLoopInput
        rnd: int,
        callbacks: Optional[PhaseOps] = None,
    ) -> dict | None:
        """Return the plan dict for this round.

        Parameters
        ----------
        inp : DevLoopInput
            Workflow input (must have ``triggering_issue``, ``project_id``,
            ``agent_label``, ``poll_interval_seconds``).
        rnd : int
            Current round number (currently unused by plan logic — reserved).
        callbacks : PhaseOps, optional
            Injected callbacks for testing.

        Returns
        -------
        dict | None
            A plan dict with an ``issues`` list, or ``None`` on failure.
        """
        cb = callbacks or PhaseOps.default()
        # Use plan_ops sub-protocol with fallback to top-level PhaseOps fields.
        plan_ops = cb.plan_ops
        _comment_cb = plan_ops.comment or cb.post_comment

        if inp.triggering_issue > 0:
            # Lightweight path: single-issue plan via activity (issue #120).
            _plan_issue_cb = plan_ops.plan_issue or cb.plan_issue
            if _plan_issue_cb is not None:
                plan = await _plan_issue_cb(
                    PlanIssueInput(
                        project_id=inp.project_id,
                        issue_number=inp.triggering_issue,
                    )
                )
            else:
                plan = await workflow.execute_activity(
                    "plan_issue",
                    PlanIssueInput(
                        project_id=inp.project_id,
                        issue_number=inp.triggering_issue,
                    ),
                    result_type=dict,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=_RETRY,
                )
        else:
            # Backlog reasoning path: dispatch Plan Agent Execution Job.
            _dispatch_plan_cb = plan_ops.dispatch_plan or cb.dispatch_plan
            if _dispatch_plan_cb is not None:
                result = await _dispatch_plan_cb(
                    inp.project_id,
                    TaskSpec(
                        phase="plan",
                        project_id=inp.project_id,
                        issue_number=inp.triggering_issue,
                        extra={"agent_label": inp.agent_label},
                    ),
                    inp.poll_interval_seconds,
                )
            else:
                result = await workflow.execute_activity(
                    "dispatch_agent_job",
                    DispatchInput(
                        inp.project_id,
                        inp.triggering_issue,
                        TaskSpec(
                            phase="plan",
                            project_id=inp.project_id,
                            issue_number=inp.triggering_issue,
                            extra={"agent_label": inp.agent_label},
                        ),
                        poll_interval_seconds=inp.poll_interval_seconds,
                    ),
                    result_type=AgentJobResult,
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                    retry_policy=RetryPolicy(maximum_attempts=3),
                    task_queue=JOB_DISPATCH_QUEUE,
                )
            plan = result.plan or {"issues": []}

        issues = plan.get("issues") or []
        _drop_cb = plan_ops.drop_issues_in_review or cb.drop_issues_in_review
        if _drop_cb is not None:
            issues = await _drop_cb(inp, issues)
        else:
            # Default: open_agent_pr_issue_numbers activity.
            in_review = await workflow.execute_activity(
                "open_agent_pr_issue_numbers",
                inp.project_id,
                result_type=list,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=_RETRY,
            )
            in_review = {cb.as_int(n) for n in (in_review or [])}
            if in_review:
                issues = [
                    issue
                    for issue in issues
                    if cb.as_int(issue.get("id")) not in in_review
                ]

        return {**plan, "issues": issues}


class PlanPhaseCallbacks(PhaseOps):
    """Backward-compatible shim that delegates to a ``PhaseOps`` instance.

    This class exists only for callers that still construct
    ``PlanPhaseCallbacks(plan_issue=..., dispatch_plan=..., ...)`` directly.  On
    construction it creates a ``PhaseOps`` that carries the same fields,
    so all downstream code uses the unified protocol.

    Subclassing ``PhaseOps`` so that consumers expecting a ``PhaseOps``
    instance still work.
    """

    def __init__(
        self,
        plan_issue: Optional[
            Callable[[PlanIssueInput], Coroutine[Any, Any, dict]]
        ] = None,
        dispatch_plan: Optional[_DispatchPlanCallback] = None,
        drop_issues_in_review: Optional[_DropInReviewCallback] = None,
        post_comment: Optional[_PostCommentCallback] = None,
        kpi_bump: Optional[_KpiBumpCallback] = None,
        **kwargs: Any,
    ) -> None:
        PhaseOps.__init__(
            self,
            plan_issue=plan_issue,
            dispatch_plan=dispatch_plan,
            drop_issues_in_review=drop_issues_in_review,
            comment=post_comment,
            kpi_bump=kpi_bump,
            **kwargs,
        )

    @classmethod
    def default(cls) -> "PlanPhaseCallbacks":
        return cls()

    @property
    def phaseops(self) -> PhaseOps:
        return self


# Re-export for convenience.
PhaseOpsCallbacks = PhaseOps  # noqa: F401
PlanPhaseCallbacks = PlanPhaseCallbacks  # noqa: F401
