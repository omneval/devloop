"""Shared callback type aliases for all Dev Loop phase modules.

This module is the single source of truth for callback type aliases
previously duplicated across 7+ phase module files.  Every file that
uses one of these aliases imports it from here instead of redefining.

Exported aliases (all are ``typing.TypeAlias`` via ``Callable``):

    _PostCommentCallback   = Callable[[str, int, str], Coroutine[Any, Any, None]]
        Post a GitHub Issue/PR comment (project_id, issue_number, body).

    _KpiBumpCallback       = Callable[[str, int], Coroutine[Any, Any, None]]
        Increment a per-issue KPI counter (key, amount).

    _CleanupCallback       = Callable[[str], Coroutine[Any, Any, None]]
        Delete the output ConfigMap for a completed job (job_name).

    _DispatchCallback      = Callable[[str, TaskSpec, int, float], Coroutine[Any, Any, AgentJobResult]]
        Dispatch an Agent Execution Job and wait for the result.

    _DispatchFixCallback   = Callable[[str, TaskSpec, int, float], Coroutine[Any, Any, int]]
        Dispatch a CI fix agent job; returns commit count (int).

    _PollCiCallback        = Callable[[str, int], Coroutine[Any, Any, CIChecksResult]]
        Poll CI checks for a PR (project_id, pr_number).

    _DispatchPlanCallback  = Callable[[str, TaskSpec, float], Coroutine[Any, Any, AgentJobResult]]
        Dispatch a plan agent job (backlog path).

    _AnswerQuestionCallback = Callable[[str, int, AgentJobResult], Coroutine[Any, Any, AgentJobResult]]
        Resolve an AWAITING_HUMAN question for an execute job.

    _DispatchReviewCallback = Callable[[str, TaskSpec, int, float], Coroutine[Any, Any, AgentJobResult]]
        Dispatch the review agent job and wait for result.

    _PostReviewFindingsCallback = Callable[[str, str, dict, AgentJobResult], Coroutine[Any, Any, None]]
        Post reviewer's findings (summary + inline comments) to the PR.

    _RequestReviewerCallback = Callable[[str, Optional[int]], Coroutine[Any, Any, ReviewerRequestResult]]
        Request a GitHub PR reviewer.

    _DropInReviewCallback  = Callable[[Any, list[dict]], Coroutine[Any, Any, list[dict]]]
        Drop issues that already have an open agent PR.

    _GetBranchCallback     = Callable[[str, int], Coroutine[Any, Any, str]]
        Resolve a PR's branch (project_id, pr_number).

    _DispatchExecuteCallback = Callable[[str, TaskSpec, int, float], Coroutine[Any, Any, AgentJobResult]]
        Dispatch the execute agent job and wait for result.

    _PlanIssueCallback     = Callable[[PlanIssueInput], Coroutine[Any, Any, dict]]
        Lightweight single-issue plan (webhook-triggered).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Coroutine, Optional

if TYPE_CHECKING:
    from ..cichecks import CIChecksResult
    from ..execution import TaskSpec
    from ..github import ReviewerRequestResult
    from ..shared import AgentJobResult, PlanIssueInput

# Use Callable directly as a type alias value (not a type annotation).
# This makes the alias importable and identity-comparable.


# ── Core I/O operations (shared by every phase) ────────────────────────── #

#: Post a GitHub Issue/PR comment (project_id, issue_number, body).
_PostCommentCallback = Callable[[str, int, str], Coroutine[Any, Any, None]]

#: Increment a per-issue KPI counter (key, amount).
_KpiBumpCallback = Callable[[str, int], Coroutine[Any, Any, None]]

#: Delete the output ConfigMap for a completed job (job_name).
_CleanupCallback = Callable[[str], Coroutine[Any, Any, None]]

#: Dispatch an Agent Execution Job and wait for the result.
_DispatchCallback = Callable[
    [str, "TaskSpec", int, float], Coroutine[Any, Any, "AgentJobResult"]
]

#: Poll CI checks for a PR (project_id, pr_number).
_PollCiCallback = Callable[[str, int], Coroutine[Any, Any, "CIChecksResult"]]

#: Request a GitHub PR reviewer.
_RequestReviewerCallback = Callable[
    [str, Optional[int]], Coroutine[Any, Any, "ReviewerRequestResult"]
]


# ── ExecutePhase-specific ──────────────────────────────────────────────── #

#: Resolve an AWAITING_HUMAN question for an execute job.
_AnswerQuestionCallback = Callable[
    [str, int, "AgentJobResult"], Coroutine[Any, Any, "AgentJobResult"]
]

#: Dispatch the execute agent job and wait for result.
_DispatchExecuteCallback = Callable[
    [str, "TaskSpec", int, float], Coroutine[Any, Any, "AgentJobResult"]
]


# ── ReviewPhase-specific ───────────────────────────────────────────────── #

#: Dispatch the review agent job and wait for result.
_DispatchReviewCallback = Callable[
    [str, "TaskSpec", int, float], Coroutine[Any, Any, "AgentJobResult"]
]

#: Post reviewer's findings (summary + inline comments) to the PR.
_PostReviewFindingsCallback = Callable[
    [str, str, dict, "AgentJobResult"], Coroutine[Any, Any, None]
]


# ── CICycle/ReviewFixPass-specific ─────────────────────────────────────── #

#: Dispatch a CI fix agent job; returns commit count (int).
_DispatchFixCallback = Callable[[str, "TaskSpec", int, float], Coroutine[Any, Any, int]]


# ── PlanPhase-specific ─────────────────────────────────────────────────── #

#: Dispatch a plan agent job (backlog path).
_DispatchPlanCallback = Callable[
    [str, "TaskSpec", float], Coroutine[Any, Any, "AgentJobResult"]
]

#: Drop issues that already have an open agent PR.
_DropInReviewCallback = Callable[[Any, list[dict]], Coroutine[Any, Any, list[dict]]]

#: Lightweight single-issue plan (webhook-triggered).
_PlanIssueCallback = Callable[["PlanIssueInput"], Coroutine[Any, Any, dict]]


# ── PRComment-specific ─────────────────────────────────────────────────── #

#: Resolve a PR's branch (project_id, pr_number).
_GetBranchCallback = Callable[[str, int], Coroutine[Any, Any, str]]
