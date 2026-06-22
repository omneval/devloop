"""PlanPhaseOps — focused callback protocol for the Plan phase.

Exposes exactly four fields that the Plan phase actually needs:

- ``comment`` — post GitHub Issue comments.
- ``plan_issue`` — lightweight single-issue plan (webhook-triggered).
- ``dispatch_plan`` — dispatch a plan agent job (backlog path).
- ``drop_issues_in_review`` — filter out issues that already have open agent PRs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Coroutine

from ..shared import AgentJobResult, PlanIssueInput, TaskSpec


# Type alias: post a GitHub Issue/PR comment.
_PostCommentCallback = Callable[[str, int, str], Coroutine[Any, Any, None]]

# Type alias: lightweight single-issue plan (webhook-triggered).
_PlanIssueCallback = Callable[[PlanIssueInput], Coroutine[Any, Any, dict]]

# Type alias: dispatch a plan agent job (backlog path).
_DispatchPlanCallback = Callable[
    [str, TaskSpec, float], Coroutine[Any, Any, AgentJobResult]
]

# Type alias: drop issues that already have an open agent PR.
_DropInReviewCallback = Callable[[Any, list[dict]], Coroutine[Any, Any, list[dict]]]


@dataclass
class PlanPhaseOps:
    """Focused callback protocol for the Plan phase.

    Each field is ``None`` by default.  When set, the phase calls the
    callback directly instead of hitting a Temporal activity.
    """

    #: Post a GitHub Issue/PR comment.
    comment: _PostCommentCallback | None = None

    #: Lightweight single-issue plan (webhook-triggered).
    plan_issue: _PlanIssueCallback | None = None

    #: Dispatch a plan agent job (backlog path).
    dispatch_plan: _DispatchPlanCallback | None = None

    #: Drop issues that already have an open agent PR.
    drop_issues_in_review: _DropInReviewCallback | None = None

    @classmethod
    def default(cls) -> "PlanPhaseOps":
        """Return an instance with all fields set to ``None``."""
        return cls()
