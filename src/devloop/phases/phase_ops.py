"""Unified PhaseOps callback protocol for all phase modules.

Promotes the informal ``_WorkflowCommon`` mixin into a formal ``PhaseOps`` seam
that every phase module references for its I/O operations.  Rather than each
phase defining its own callback dataclass, all phases share one protocol that
covers every operation — ``comment``, ``cleanup``, ``dispatch``, ``kpi_bump``,
``poll_ci``, ``request_reviewer``, and all phase-specific operations.

Each field is an optional callable.  When a field is ``None`` the phase falls
back to its default Temporal activity path.  Phases simply reference the fields
they need and leave the rest as ``None``.

The ``DevLoopWorkflow`` and ``PRCommentWorkflow`` implement this protocol by
delegating to their PhaseOps methods wrapped in ``async def`` callables.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Callable, Coroutine, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

from ..cichecks import CIChecksResult, PollCIChecksInput
from ..execution import (
    AgentJobResult,
    DispatchInput,
    TaskSpec,
    WorkflowKpiInput,
)
from ..github import (
    GithubNotificationInput,
    PlanIssueInput,
    RequestReviewerInput,
    ReviewerRequestResult,
)
from .._constants import (
    JOB_DISPATCH_QUEUE,
    _ACTIVITY_TIMEOUT,
    _GITHUB_COMMENT_TIMEOUT,
    _RETRY,
)


# ── Core I/O operations (shared by every phase) ────────────────────────── #

_PostCommentCallback = Callable[[str, int, str], Coroutine[Any, Any, None]]
_CleanupCallback = Callable[[str], Coroutine[Any, Any, None]]
_DispatchCallback = Callable[
    [str, TaskSpec, int, float], Coroutine[Any, Any, AgentJobResult]
]
_KpiBumpCallback = Callable[[str, int], Coroutine[Any, Any, None]]
_KpiTakeCallback = Callable[[], Coroutine[Any, Any, dict]]
_EmitKpisCallback = Callable[[WorkflowKpiInput], Coroutine[Any, Any, None]]
_PollCiCallback = Callable[[str, int], Coroutine[Any, Any, CIChecksResult]]
_RequestReviewerCallback = Callable[
    [str, Optional[int]], Coroutine[Any, Any, ReviewerRequestResult]
]

# ── ExecutePhase-specific ──────────────────────────────────────────────── #

_AnswerQuestionCallback = Callable[
    [str, int, AgentJobResult], Coroutine[Any, Any, AgentJobResult]
]

# ── ReviewPhase-specific ───────────────────────────────────────────────── #

_PostReviewFindingsCallback = Callable[
    [str, str, dict, AgentJobResult], Coroutine[Any, Any, None]
]

# ── CICycle/ReviewFixPass-specific ─────────────────────────────────────── #

# dispatch_fix — same positional shape as _DispatchCallback but returns
# commit count instead of AgentJobResult (#188).
_DispatchFixCallback = Callable[
    [str, TaskSpec, int, float], Coroutine[Any, Any, int]
]  # returns commits count

# ── PlanPhase-specific ──────────────────────────────────────────────────── #

_DispatchPlanCallback = Callable[
    [str, TaskSpec, float], Coroutine[Any, Any, AgentJobResult]
]
_DropInReviewCallback = Callable[[Any, list[dict]], Coroutine[Any, Any, list[dict]]]


class PhaseOps:
    """Unified I/O adapter protocol for all phase modules.

    Every field is an optional callable.  When a field is ``None`` the
    calling phase falls back to its default Temporal activity path.
    Phases only reference the fields they actually need.
    """

    # ── Core operations (shared by every phase) ──────────────────────── #

    #: Post a GitHub Issue/PR comment.
    #: Also accessible via the backward-compatible ``post_comment`` alias.
    #: NOTE: class-level type annotation removed to avoid ``ty`` union-type
    #: collision with the ``comment`` method (the ``__init__`` assigns this
    #: instance attribute at runtime).
    comment = None

    #: Delete the output ConfigMap for a completed job.
    #: NOTE: same rationale as ``comment`` above.
    cleanup = None

    #: Dispatch an Agent Execution Job and wait for the result.
    dispatch: Optional[_DispatchCallback] = None

    #: Increment a per-issue KPI counter.
    kpi_bump: Optional[_KpiBumpCallback] = None

    #: Return and reset the accumulated KPI counters (one issue's worth).
    kpi_take: Optional[_KpiTakeCallback] = None

    #: Emit KPIs via the ``emit_workflow_kpis`` activity.
    emit_kpis: Optional[_EmitKpisCallback] = None

    #: Poll CI checks for a PR.
    poll_ci: Optional[_PollCiCallback] = None

    #: Request a GitHub PR reviewer.
    #: NOTE: same rationale as ``comment`` above.
    request_reviewer = None

    # ── ExecutePhase-specific ────────────────────────────────────────── #

    #: Dispatch the execute agent job.
    dispatch_execute: Optional[_DispatchCallback] = None

    #: Resolve an ``AWAITING_HUMAN`` question for an execute job.
    answer_question: Optional[_AnswerQuestionCallback] = None

    # ── ReviewPhase-specific ─────────────────────────────────────────── #

    #: Dispatch the review agent job.
    dispatch_review: Optional[_DispatchCallback] = None

    #: Post the reviewer's findings to the PR (summary + inline comments).
    post_review_findings: Optional[_PostReviewFindingsCallback] = None

    # ── CICycle / ReviewFixPass-specific ─────────────────────────────── #

    #: Dispatch a CI fix agent job.  Same positional shape as ``dispatch``
    #: but returns an int (commit count) rather than ``AgentJobResult``.
    dispatch_fix: Optional[_DispatchFixCallback] = None

    # ── PlanPhase-specific ───────────────────────────────────────────── #

    #: Plan a single issue (lightweight path, webhook-triggered).
    plan_issue: Optional[Callable[[PlanIssueInput], Coroutine[Any, Any, dict]]] = None

    #: Dispatch a plan agent job (backlog path).
    dispatch_plan: Optional[_DispatchPlanCallback] = None

    #: Drop issues that already have an open agent PR.
    drop_issues_in_review: Optional[_DropInReviewCallback] = None

    def __init__(
        self,
        comment: Optional[_PostCommentCallback] = None,
        cleanup: Optional[_CleanupCallback] = None,
        dispatch: Optional[_DispatchCallback] = None,
        kpi_bump: Optional[_KpiBumpCallback] = None,
        kpi_take: Optional[_KpiTakeCallback] = None,
        emit_kpis: Optional[_EmitKpisCallback] = None,
        poll_ci: Optional[_PollCiCallback] = None,
        request_reviewer: Optional[_RequestReviewerCallback] = None,
        dispatch_execute: Optional[_DispatchCallback] = None,
        answer_question: Optional[_AnswerQuestionCallback] = None,
        dispatch_review: Optional[_DispatchCallback] = None,
        post_review_findings: Optional[_PostReviewFindingsCallback] = None,
        dispatch_fix: Optional[_DispatchFixCallback] = None,
        plan_issue: Optional[
            Callable[[PlanIssueInput], Coroutine[Any, Any, dict]]
        ] = None,
        dispatch_plan: Optional[_DispatchPlanCallback] = None,
        drop_issues_in_review: Optional[_DropInReviewCallback] = None,
        # ── Backward-compatible aliases ──────────────────────────── #
        post_comment: Optional[_PostCommentCallback] = None,
    ) -> None:
        """Initialize PhaseOps fields.

        ``post_comment`` is accepted as a backward-compatible alias for
        ``comment``.  If both are provided, ``post_comment`` takes
        precedence (it is the older name used by tests).
        """
        self.comment = post_comment if post_comment is not None else comment
        self._phase_comment_callback: Optional[_PostCommentCallback] = self.comment
        self.cleanup = cleanup
        self._phase_cleanup_callback: Optional[_CleanupCallback] = self.cleanup
        self.dispatch = dispatch
        self.kpi_bump = kpi_bump
        self.kpi_take = kpi_take
        self.emit_kpis = emit_kpis
        self.poll_ci = poll_ci
        self.request_reviewer = request_reviewer
        self._phase_request_reviewer_callback: Optional[_RequestReviewerCallback] = (
            self.request_reviewer
        )
        self.dispatch_execute = dispatch_execute
        self.answer_question = answer_question
        self.dispatch_review = dispatch_review
        self.post_review_findings = post_review_findings
        self.dispatch_fix = dispatch_fix
        self.plan_issue = plan_issue
        self.dispatch_plan = dispatch_plan
        self.drop_issues_in_review = drop_issues_in_review

    @property
    def post_comment(self) -> Optional[_PostCommentCallback]:
        """Backward-compatible alias for the comment callback."""
        return self._phase_comment_callback

    @post_comment.setter
    def post_comment(self, value: Optional[_PostCommentCallback]) -> None:
        self._phase_comment_callback = value

    @property
    def phaseops(self) -> "PhaseOps":
        """Backward-compatible alias for ``self``.

        Shim classes implement ``.phaseops`` as a property.  Since
        ``PhaseOps`` is the protocol itself, it simply returns itself.
        """
        return self

    @classmethod
    def default(cls) -> "PhaseOps":
        """Return a PhaseOps instance with every field set to ``None``.

        When all fields are ``None`` each phase falls back to its default
        Temporal activity path.
        """
        return cls()

    # ------------------------------------------------------------------
    # as_int
    # ------------------------------------------------------------------

    def as_int(self, value: Any) -> int:
        """Safely convert *value* to ``int``, returning ``0`` on failure."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------
    # pr_number_from_url (static)
    # ------------------------------------------------------------------

    @staticmethod
    def pr_number_from_url(url: Any) -> int:
        """Extract PR number from a GitHub URL, returning ``0`` on failure."""
        if not url or not isinstance(url, str):
            return 0
        match = re.search(r"/pull/(\d+)", url)
        if match:
            return int(match.group(1))
        return 0

    # ------------------------------------------------------------------
    # comment — default Temporal activity path (fallback when field is None)
    # ------------------------------------------------------------------

    async def _phase_comment(  # noqa: F811
        self,
        project_id: str,
        issue_number: int,
        body: str,
        *,
        callback: Optional[Callable[[str, int, str], Coroutine[Any, Any, None]]] = None,
    ) -> None:
        """Post a GitHub Issue / PR comment.

        When *callback* is provided it is called directly; otherwise the
        ``post_github_comment`` Temporal activity is invoked.
        """
        if callback is not None:
            await callback(project_id, issue_number, body)
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

    # ------------------------------------------------------------------
    # cleanup — default Temporal activity path (fallback when field is None)
    # ------------------------------------------------------------------

    async def _phase_cleanup(  # noqa: F811
        self,
        job_name: str,
        *,
        callback: Optional[Callable[[str], Coroutine[Any, Any, None]]] = None,
    ) -> None:
        """Delete the output ConfigMap for a completed job.

        Fire-and-forget: failures are logged, never raised.
        """
        if callback is not None:
            await callback(job_name)
            return
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

    # ------------------------------------------------------------------
    # _dispatch_helper
    # ------------------------------------------------------------------

    async def dispatch_helper(
        self,
        project_id: str,
        spec: Any,  # TaskSpec
        issue_number: int,
        poll_interval_seconds: float,
        *,
        dispatch_callback: Optional[
            Callable[[str, Any, int, float], Coroutine[Any, Any, AgentJobResult]]
        ] = None,
        activity_name: str = "dispatch_agent_job",
        task_queue: Optional[str] = JOB_DISPATCH_QUEUE,
    ) -> AgentJobResult:
        """Generic dispatch: check callback first, fall back to Temporal activity.

        Parameters
        ----------
        project_id : str
            Target repository owner / name.
        spec : TaskSpec
            The task specification to pass to the dispatch activity.
        issue_number : int
            GitHub issue number.
        poll_interval_seconds : float
            How often to poll the job for status.
        dispatch_callback : callable, optional
            When provided it is invoked directly with the same arguments.
        activity_name : str
            Temporal activity name (default ``dispatch_agent_job``).
        task_queue : str, optional
            Temporal task queue (default ``None`` → worker default).
        """
        if dispatch_callback is not None:
            return await dispatch_callback(
                project_id, spec, issue_number, poll_interval_seconds
            )
        return await workflow.execute_activity(
            activity_name,
            DispatchInput(
                project_id,
                issue_number,
                spec,
                poll_interval_seconds=poll_interval_seconds,
            ),
            result_type=AgentJobResult,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3),
            task_queue=task_queue,
        )

    # ------------------------------------------------------------------
    # poll (CI checks)
    # ------------------------------------------------------------------

    async def poll(
        self,
        project_id: str,
        pr_number: int,
        *,
        callback: Optional[
            Callable[[str, int], Coroutine[Any, Any, CIChecksResult]]
        ] = None,
    ) -> CIChecksResult:
        """Poll CI checks for a pull request.

        When *callback* is provided it is called directly; otherwise the
        ``poll_ci_checks`` Temporal activity is invoked.
        """
        if callback is not None:
            return await callback(project_id, pr_number)
        return await workflow.execute_activity(
            "poll_ci_checks",
            PollCIChecksInput(project_id=project_id, pr_number=pr_number),
            result_type=CIChecksResult,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

    # ------------------------------------------------------------------
    # request_reviewer — default Temporal activity path (fallback when field is None)
    # ------------------------------------------------------------------

    async def _phase_request_reviewer(  # noqa: F811
        self,
        project_id: str,
        pr_number: int,
        *,
        callback: Optional[
            Callable[[str, int], Coroutine[Any, Any, ReviewerRequestResult]]
        ] = None,
    ) -> ReviewerRequestResult:
        """Request a GitHub PR reviewer.

        When *callback* is provided it is called directly; otherwise the
        ``request_github_reviewer`` Temporal activity is invoked.
        The reviewer parameter is left empty so the activity resolves it
        from the project registry.
        """
        if callback is not None:
            return await callback(project_id, pr_number)
        return await workflow.execute_activity(
            "request_github_reviewer",
            RequestReviewerInput(
                project_id=project_id, pr_number=pr_number, reviewer=""
            ),
            result_type=ReviewerRequestResult,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

    # ------------------------------------------------------------------
    # _comment — injectable callback with Temporal activity fallback
    # ------------------------------------------------------------------

    async def _comment(
        self,
        project_id: str,
        issue_number: int,
        body: str,
    ) -> None:
        """Post a GitHub Issue/PR comment via the injectable ``comment``
        callback, falling back to the ``post_github_comment`` Temporal
        activity when the callback is unset.
        """
        if self.comment is not None:
            await self.comment(project_id, issue_number, body)
            return
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

    # ------------------------------------------------------------------
    # _dispatch — injectable callback with Temporal activity fallback
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_number: int = 0,
        poll_interval_seconds: float = 5.0,
    ) -> AgentJobResult:
        """Dispatch an Agent Execution Job via the injectable ``dispatch``
        callback, falling back to the ``dispatch_agent_job`` Temporal
        activity when the callback is unset.
        """
        if self.dispatch is not None:
            return await self.dispatch(
                project_id, spec, issue_number, poll_interval_seconds
            )
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

    # ------------------------------------------------------------------
    # _cleanup — injectable callback with Temporal activity fallback
    # ------------------------------------------------------------------

    async def _cleanup(
        self,
        job_name: str,
    ) -> None:
        """Delete the output ConfigMap for a completed job via the
        injectable ``cleanup`` callback, falling back to the
        ``cleanup_configmap`` Temporal activity when the callback is unset.

        Fire-and-forget: failures are logged, never raised.
        """
        if self.cleanup is not None:
            await self.cleanup(job_name)
            return
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

    # ------------------------------------------------------------------
    # _request_reviewer — injectable callback with Temporal activity fallback
    # ------------------------------------------------------------------

    async def _request_reviewer(
        self,
        project_id: str,
        pr_number: int | None,
    ) -> ReviewerRequestResult:
        """Request a GitHub PR reviewer via the injectable
        ``request_reviewer`` callback, falling back to the
        ``request_github_reviewer`` Temporal activity when the callback
        is unset.
        """
        if self.request_reviewer is not None:
            return await self.request_reviewer(project_id, pr_number)
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

    # ------------------------------------------------------------------
    # _emit_kpis — injectable callback with Temporal activity fallback
    # ------------------------------------------------------------------

    async def _emit_kpis(
        self,
        inp: WorkflowKpiInput,
    ) -> None:
        """Emit workflow KPIs via the injectable ``emit_kpis`` callback,
        falling back to the ``emit_workflow_kpis`` Temporal activity when
        the callback is unset.

        Strictly best-effort: a telemetry hiccup must never fail or
        retry-storm the workflow.
        """
        if self.emit_kpis is not None:
            await self.emit_kpis(inp)
            return
        try:
            await workflow.execute_activity(
                "emit_workflow_kpis",
                inp,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except Exception:  # noqa: BLE001
            workflow.logger.warning("emit_workflow_kpis failed (ignored)")

    # ------------------------------------------------------------------
    # _kpi_bump / _kpi_take — per-issue KPI counter helpers
    # ------------------------------------------------------------------

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
