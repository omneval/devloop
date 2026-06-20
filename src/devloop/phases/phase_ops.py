"""Unified PhaseOps callback protocol for all phase modules.

Promotes the informal `_WorkflowCommon` mixin into a formal ``PhaseOps`` seam
that every phase module references for its I/O operations.  Rather than each
phase defining its own callback dataclass, all phases share one protocol that
covers every operation — ``comment``, ``cleanup``, ``dispatch``, ``kpi_bump``,
``poll_ci``, ``request_reviewer``, and all phase-specific operations.

Each field is an optional callable.  When a field is ``None`` the phase falls
back to its default Temporal activity path.  Phases simply reference the fields
they need and leave the rest as ``None``.

The ``DevLoopWorkflow`` and ``PRCommentWorkflow`` implement this protocol by
delegating to their ``_WorkflowCommon`` methods wrapped in ``async def`` callables.
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine, Optional

from ..cichecks import CIChecksResult
from ..execution import AgentJobResult, TaskSpec, WorkflowKpiInput
from ..github import PlanIssueInput, ReviewerRequestResult


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

# dispatch_fix has the same shape as the old CICycle._Callbacks.dispatch_fix
_DispatchFixCallback = Callable[
    [str, int, dict, float], Coroutine[Any, Any, int]
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
    comment: Optional[_PostCommentCallback] = None

    #: Delete the output ConfigMap for a completed job.
    cleanup: Optional[_CleanupCallback] = None

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
    request_reviewer: Optional[_RequestReviewerCallback] = None

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

    #: Dispatch a CI fix agent job.  Signature differs from ``dispatch``
    #: because CICycle passes a spec *dict* rather than a ``TaskSpec``.
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
        self.cleanup = cleanup
        self.dispatch = dispatch
        self.kpi_bump = kpi_bump
        self.kpi_take = kpi_take
        self.emit_kpis = emit_kpis
        self.poll_ci = poll_ci
        self.request_reviewer = request_reviewer
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
        """Backward-compatible alias for ``comment``."""
        return self.comment

    @post_comment.setter
    def post_comment(self, value: Optional[_PostCommentCallback]) -> None:
        self.comment = value

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
