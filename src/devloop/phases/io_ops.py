"""Deep I/O adapter module for devloop phase operations.

This module contains the ``IoOps`` mixin that concentrates all I/O helper
methods for phase operations.  Each method is an async default that
delegates to injectable callbacks (when present) or falls back to Temporal
activity calls — the same pattern that ``PhaseOps`` used before this
extraction.

The ``IoOps`` mixin is the *deep* layer: callers only need to know the
``PhaseOps`` protocol, while the actual I/O machinery (Temporal activity
shapes, retry policies, timeout configurations) lives here and can be
overridden via the callback fields on ``PhaseOps``.

Extracted methods:

* ``_comment`` — post a GitHub Issue/PR comment
* ``_dispatch`` — dispatch an Agent Execution Job
* ``_cleanup`` — delete the output ConfigMap for a completed job
* ``_request_reviewer`` — request a GitHub PR reviewer
* ``_emit_kpis`` — emit workflow KPIs via the ``emit_workflow_kpis`` activity
* ``_kpi_bump`` / ``_kpi_take`` — per-issue KPI counter helpers
* ``_phase_comment`` — comment with explicit callback parameter
* ``_phase_request_reviewer`` — request reviewer with explicit callback parameter
* ``poll`` — poll CI checks for a PR
* ``dispatch_helper`` — generic dispatch with configurable activity name
* ``as_int`` — safe integer conversion
* ``pr_number_from_url`` — extract PR number from a GitHub URL
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Callable, Coroutine, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

from .._constants import (
    JOB_DISPATCH_QUEUE,
    _ACTIVITY_TIMEOUT,
    _GITHUB_COMMENT_TIMEOUT,
    _RETRY,
)
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

# ── Core I/O callback type aliases (re-exported for phase modules) ────── #

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

_DispatchFixCallback = Callable[
    [str, TaskSpec, int, float], Coroutine[Any, Any, int]
]  # returns commits count

# ── PlanPhase-specific ──────────────────────────────────────────────────── #

_DispatchPlanCallback = Callable[
    [str, TaskSpec, float], Coroutine[Any, Any, AgentJobResult]
]
_DropInReviewCallback = Callable[[Any, list[dict]], Coroutine[Any, Any, list[dict]]]


class IoOps:
    """Mixin providing the deep I/O adapter methods for phase operations.

    Every method is an async default with a callback-first / Temporal-fallback
    pattern.  When ``PhaseOps`` subclasses ``IoOps`` and overrides a field
    (``comment``, ``dispatch``, etc.) via the callback seam, the override
    wins — preserving injectability.

    Callback fields — when non-None, these take precedence over Temporal
    activity calls inside the I/O helpers below.  Set them on the concrete
    ``PhaseOps`` instance (or any subtype) via the dataclass ``__init__`` or
    keyword arguments at construction time.
    """

    # Each callback field is ``None`` by default.  IoOps methods read these
    # via ``self.<field>``; because ``IoOps`` declares them here with
    # ``Optional`` type, static type-checkers are happy, and at runtime the
    # ``PhaseOps`` dataclass ``__init__`` will assign the actual callbacks
    # when the user passes one.  When a ``PhaseOps`` instance is built *without*
    # a callback (i.e. only the ``IoOps`` mixin is present), ``self.comment``
    # simply resolves to ``None`` and the Temporal-fallback path is taken.

    # ── Core callback fields ────────────────────────────────────────── #
    # Declared here so static type-checkers are happy; IoOps methods
    # read these via ``self.<field>``.  When PhaseOps is instantiated,
    # ``__init__`` assigns the actual callbacks (or leaves them None).

    comment: Optional[_PostCommentCallback] = None
    cleanup: Optional[_CleanupCallback] = None
    dispatch: Optional[_DispatchCallback] = None
    request_reviewer: Optional[_RequestReviewerCallback] = None
    emit_kpis: Optional[_EmitKpisCallback] = None
    poll_ci: Optional[_PollCiCallback] = None
    kpi_bump: Optional[_KpiBumpCallback] = None
    kpi_take: Optional[_KpiTakeCallback] = None

    # Internal callback refs preserved by PhaseOps for the "_phase_*"
    # helper methods.  These mirror the fields above so that the methods
    # _phase_comment, _phase_cleanup, _phase_request_reviewer can read
    # a stable value set at construction time.

    _phase_comment_callback: Optional[_PostCommentCallback] = None
    _phase_cleanup_callback: Optional[_CleanupCallback] = None
    _phase_request_reviewer_callback: Optional[_RequestReviewerCallback] = None

    # ── ExecutePhase callbacks ─────────────────────────────────────── #
    dispatch_execute: Optional[_DispatchCallback] = None
    answer_question: Optional[_AnswerQuestionCallback] = None

    # ── ReviewPhase callbacks ──────────────────────────────────────── #
    dispatch_review: Optional[_DispatchCallback] = None
    post_review_findings: Optional[_PostReviewFindingsCallback] = None

    # ── CICycle callbacks ──────────────────────────────────────────── #
    dispatch_fix: Optional[_DispatchFixCallback] = None

    # ── PlanPhase callbacks ────────────────────────────────────────── #
    plan_issue: Optional[Callable[[PlanIssueInput], Coroutine[Any, Any, dict]]] = None
    dispatch_plan: Optional[_DispatchPlanCallback] = None
    drop_issues_in_review: Optional[_DropInReviewCallback] = None

    def __init__(
        self,
        comment: Optional[_PostCommentCallback] = None,
        cleanup: Optional[_CleanupCallback] = None,
        dispatch: Optional[_DispatchCallback] = None,
        request_reviewer: Optional[_RequestReviewerCallback] = None,
        emit_kpis: Optional[_EmitKpisCallback] = None,
        poll_ci: Optional[_PollCiCallback] = None,
        kpi_bump: Optional[_KpiBumpCallback] = None,
        kpi_take: Optional[_KpiTakeCallback] = None,
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
    ) -> None:
        """Assign every callback field so that IoOps methods can read
        ``self.<field>`` without falling back to the class-default ``None``.
        """
        self.comment = comment
        self._phase_comment_callback = comment
        self.cleanup = cleanup
        self._phase_cleanup_callback = cleanup
        self.dispatch = dispatch
        self.request_reviewer = request_reviewer
        self._phase_request_reviewer_callback = request_reviewer
        self.emit_kpis = emit_kpis
        self.poll_ci = poll_ci
        self.kpi_bump = kpi_bump
        self.kpi_take = kpi_take
        self.dispatch_execute = dispatch_execute
        self.answer_question = answer_question
        self.dispatch_review = dispatch_review
        self.post_review_findings = post_review_findings
        self.dispatch_fix = dispatch_fix
        self.plan_issue = plan_issue
        self.dispatch_plan = dispatch_plan
        self.drop_issues_in_review = drop_issues_in_review

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
            self._kpi_counters = counters  # type: ignore[attr-defined]
        counters[key] = counters.get(key, 0) + n

    def _kpi_take(self) -> dict:
        """Return and reset the accumulated counters (one issue's worth)."""
        counters = getattr(self, "_kpi_counters", None) or {}
        self._kpi_counters = {}  # type: ignore[attr-defined]
        return counters

    # ------------------------------------------------------------------
    # _phase_comment — explicit callback parameter path
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
    # _phase_request_reviewer — explicit callback parameter path
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
    # poll — CI checks
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
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

    # ------------------------------------------------------------------
    # dispatch_helper — generic dispatch
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
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=3),
            task_queue=task_queue,
        )

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
