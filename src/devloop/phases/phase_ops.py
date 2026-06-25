"""Unified PhaseOps callback protocol for all phase modules.

Promotes the informal ``_WorkflowCommon`` mixin into a formal ``PhaseOps``
seam.  The I/O adapter methods have been extracted to the deep ``IoOps``
module.  Four focused per-phase sub-protocols delegate to it.

Backwards-compatible callback aliases are re-exported from ``io_ops`` so that
existing consumers that import them from ``phase_ops`` continue to work.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .io_ops import (  # noqa: E402
    IoOps,
    # Re-export all callback type aliases for backward compatibility.
    # Consumers that previously imported from ``phase_ops`` can keep doing so.
    _AnswerQuestionCallback,  # noqa: F401
    _CleanupCallback,  # noqa: F401
    _DispatchCallback,  # noqa: F401
    _DispatchFixCallback,  # noqa: F401
    _DispatchPlanCallback,  # noqa: F401
    _DropInReviewCallback,  # noqa: F401
    _EmitKpisCallback,  # noqa: F401
    _KpiBumpCallback,  # noqa: F401
    _KpiTakeCallback,  # noqa: F401
    _PollCiCallback,  # noqa: F401
    _PostCommentCallback,  # noqa: F401
    _PostReviewFindingsCallback,  # noqa: F401
    _RequestReviewerCallback,  # noqa: F401
)
from temporalio import workflow  # noqa: F401  # backward compat re-export


class PhaseOps(IoOps):
    """Composite I/O protocol — 17 callback fields + 4 sub-protocol refs."""

    execute_ops: Any  # ExecutePhaseOps — deferred to avoid circular import
    review_ops: Any  # ReviewPhaseOps
    ci_ops: Any  # CICycleOps
    plan_ops: Any  # PlanPhaseOps

    def __init__(
        self,
        comment: Optional["_PostCommentCallback"] = None,
        cleanup: Optional["_CleanupCallback"] = None,
        dispatch: Optional["_DispatchCallback"] = None,
        request_reviewer: Optional["_RequestReviewerCallback"] = None,
        emit_kpis: Optional["_EmitKpisCallback"] = None,
        poll_ci: Optional["_PollCiCallback"] = None,
        kpi_bump: Optional["_KpiBumpCallback"] = None,
        kpi_take: Optional["_KpiTakeCallback"] = None,
        dispatch_execute: Optional["_DispatchCallback"] = None,
        answer_question: Optional["_AnswerQuestionCallback"] = None,
        dispatch_review: Optional["_DispatchCallback"] = None,
        post_review_findings: Optional["_PostReviewFindingsCallback"] = None,
        dispatch_fix: Optional["_DispatchFixCallback"] = None,
        plan_issue: Optional[Callable[..., Any]] = None,
        dispatch_plan: Optional["_DispatchPlanCallback"] = None,
        drop_issues_in_review: Optional["_DropInReviewCallback"] = None,
        post_comment: Optional["_PostCommentCallback"] = None,
        execute_ops: Any = None,
        review_ops: Any = None,
        ci_ops: Any = None,
        plan_ops: Any = None,
    ) -> None:
        super().__init__(
            comment=comment,
            cleanup=cleanup,
            dispatch=dispatch,
            request_reviewer=request_reviewer,
            emit_kpis=emit_kpis,
            poll_ci=poll_ci,
            kpi_bump=kpi_bump,
            kpi_take=kpi_take,
            dispatch_execute=dispatch_execute,
            answer_question=answer_question,
            dispatch_review=dispatch_review,
            post_review_findings=post_review_findings,
            dispatch_fix=dispatch_fix,
            plan_issue=plan_issue,
            dispatch_plan=dispatch_plan,
            drop_issues_in_review=drop_issues_in_review,
        )
        if post_comment is not None and self.comment is None:
            self.comment, self._phase_comment_callback = post_comment, post_comment
        # Build sub-protocols (deferred imports avoid circular deps).
        from .ci_cycle_ops import CICycleOps
        from .execute_phase_ops import ExecutePhaseOps
        from .plan_phase_ops import PlanPhaseOps
        from .review_phase_ops import ReviewPhaseOps

        self.execute_ops = execute_ops or ExecutePhaseOps(
            comment=self.comment,
            dispatch_execute=self.dispatch_execute,
            answer_question=self.answer_question,
            kpi_bump=self.kpi_bump,
        )
        self.review_ops = review_ops or ReviewPhaseOps(
            comment=self.comment,
            dispatch_review=self.dispatch_review,
            post_review_findings=self.post_review_findings,
            cleanup=self.cleanup,
        )
        self.ci_ops = ci_ops or CICycleOps(
            comment=self.comment,
            dispatch_fix=self.dispatch_fix,
            poll_ci=self.poll_ci,
            kpi_bump=self.kpi_bump,
            cleanup=self.cleanup,
        )
        self.plan_ops = plan_ops or PlanPhaseOps(
            comment=self.comment,
            plan_issue=self.plan_issue,
            dispatch_plan=self.dispatch_plan,
            drop_issues_in_review=self.drop_issues_in_review,
        )

    @property
    def post_comment(self) -> Optional["_PostCommentCallback"]:
        """Backward-compatible alias for the comment callback."""
        return self._phase_comment_callback

    @post_comment.setter
    def post_comment(self, value: Optional["_PostCommentCallback"]) -> None:
        self._phase_comment_callback = value

    @property
    def phaseops(self) -> "PhaseOps":
        """Alias for ``self`` — shim classes use ``.phaseops``."""
        return self

    @classmethod
    def default(cls) -> "PhaseOps":
        """Return a PhaseOps instance with every field set to ``None``."""
        return cls()
