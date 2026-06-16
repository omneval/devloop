"""Backward-compatible re-exports for devloop sub-modules.

Every module in the project imports shared, but callers only need a handful
of types.  This file re-exports all symbols from the four deep sub-modules
(execution, github, cichecks, phases) so that old imports like
``from devloop.shared import TaskSpec`` continue to work.

New code should import directly from the sub-module:

    from devloop.execution import TaskSpec, AgentJobResult
    from devloop.github import CreateGithubIssueInput, GithubNotificationInput
    from devloop.cichecks import CICheckFailure, CIChecksResult, PollCIChecksInput
    from devloop.phases import Phase, JobStatus

Constants defined *in this file* (not re-exported from sub-modules):

* ``ORCHESTRATION_QUEUE`` — Temporal task queue for the orchestration workflow.
* ``JOB_DISPATCH_QUEUE`` — Dedicated queue for Agent Execution Job dispatches.
* ``KEY_RESULT`` — ConfigMap key for the ``AgentJobResult`` payload.
* ``KEY_HUMAN_ANSWER`` — ConfigMap key for a human mid-run reply.
"""

from __future__ import annotations

import os

# ── Constants (defined here, not in sub-modules) ─────────────────────────

ORCHESTRATION_QUEUE = os.getenv("ORCHESTRATION_QUEUE", "devloop-orchestration")
JOB_DISPATCH_QUEUE = os.getenv("JOB_DISPATCH_QUEUE", "devloop-job-dispatch")
KEY_RESULT = "result"
KEY_HUMAN_ANSWER = "human_answer"

# ── Re-export all types from sub-modules ─────────────────────────────────

# execution
from devloop.execution import (
    AnswerInput,
    AgentJobResult,
    AwaitInput,
    DispatchInput,
    OpenAgentPRsInput,
    PollPRChecksInput,
    TaskSpec,
    WorkflowKpiInput,
)

# phases
from devloop.phases import JobStatus, Phase

# github
from devloop.github import (
    CreateGithubIssueInput,
    GetPRBranchInput,
    GetPRDiffInput,
    GithubNotificationInput,
    InlineComment,
    PlanIssueInput,
    PostCommentsInput,
    PublishSummaryInput,
    RequestReviewerInput,
    ReviewerRequestResult,
    UpdateGithubIssueInput,
)

# cichecks
from devloop.cichecks import (
    CICheckFailure,
    CIChecksResult,
    PollCIChecksInput,
)

__all__ = [
    # constants
    "ORCHESTRATION_QUEUE",
    "JOB_DISPATCH_QUEUE",
    "KEY_RESULT",
    "KEY_HUMAN_ANSWER",
    # execution
    "TaskSpec",
    "AgentJobResult",
    "DispatchInput",
    "OpenAgentPRsInput",
    "AnswerInput",
    "AwaitInput",
    "PollPRChecksInput",
    "WorkflowKpiInput",
    # phases
    "Phase",
    "JobStatus",
    # github
    "InlineComment",
    "PostCommentsInput",
    "GithubNotificationInput",
    "RequestReviewerInput",
    "ReviewerRequestResult",
    "GetPRBranchInput",
    "GetPRDiffInput",
    "CreateGithubIssueInput",
    "UpdateGithubIssueInput",
    "PublishSummaryInput",
    "PlanIssueInput",
    # cichecks
    "CICheckFailure",
    "CIChecksResult",
    "PollCIChecksInput",
]
