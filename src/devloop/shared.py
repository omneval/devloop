"""Sandbox-safe data structures shared between workflows and activities.

This module is imported by both Temporal workflow definitions (which run in
the deterministic sandbox) and activity code, so it must only import from the
standard library — no I/O, no threading, no network clients.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from enum import Enum


# Task queues — override via env vars to match helm chart values.
ORCHESTRATION_QUEUE = os.getenv("ORCHESTRATION_QUEUE", "devloop-orchestration")
# Dedicated queue for Agent Execution Job dispatches and LLM-bearing activities.
# A separate Worker listens here with a configurable max_concurrent_activities
# to enforce a global concurrency cap across all workflow types and projects.
JOB_DISPATCH_QUEUE = os.getenv("JOB_DISPATCH_QUEUE", "devloop-job-dispatch")

# Agent Job output ConfigMap contract: the keys the worker and the Agent
# Execution Job exchange through the Job's output ConfigMap. Defined here so both
# the devloop-temporal-worker and devloop-agent-base images reference one source.
KEY_RESULT = "result"  # the JSON-encoded AgentJobResult payload
KEY_HUMAN_ANSWER = "human_answer"  # a human's mid-run reply patched back in


class Phase(str, Enum):
    PLAN = "plan"
    EXECUTE = "execute"
    REVIEW = "review"
    DIAGNOSIS = "diagnosis"
    CI_FIX = "ci_fix"
    SUMMARIZE = "summarize"
    ANSWER = "answer"
    PR_COMMENT = "pr_comment"
    CODE_QUALITY_SCAN = "code_quality_scan"
    CODE_QUALITY_IMPROVE = "code_quality_improve"


class JobStatus(str, Enum):
    COMPLETE = "complete"
    FAILED = "failed"
    AWAITING_HUMAN = "awaiting_human"


# ---------------------------------------------------------------------------
# GitHub activity I/O
# ---------------------------------------------------------------------------


@dataclass
class InlineComment:
    file: str
    line: int
    body: str


@dataclass
class PostCommentsInput:
    """Reviewer findings posted to a PR: a PR-level ``summary`` plus optional
    line-anchored ``inline_comments``. Built by the workflow from the review
    Agent Execution Job's ``review`` payload, consumed by the ``post_pr_comments``
    activity."""

    project_id: str
    pr_number: int
    summary: str
    inline_comments: list[InlineComment] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Execution model
# ---------------------------------------------------------------------------


@dataclass
class TaskSpec:
    """The instruction payload handed to an Agent Execution Job.

    Serialized into the Job's ``TASK_SPEC`` env var by the worker and rebuilt by
    the agent entrypoint — both via the methods below, so the field set has one
    owner."""

    phase: str
    project_id: str
    issue_number: int = 0
    title: str = ""
    body: str = ""
    branch: str = ""
    instructions: str = ""
    # phase-specific extras (review rubric, merge branch list, alert payload …)
    extra: dict = field(default_factory=dict)

    def to_env_value(self) -> str:
        """Render the ``TASK_SPEC`` env value the Agent Execution Job reads."""
        return json.dumps(asdict(self))

    @classmethod
    def from_env(cls, raw: str) -> "TaskSpec":
        """Rebuild a TaskSpec from the ``TASK_SPEC`` env value (agent side)."""
        d = json.loads(raw or "{}")
        return cls(
            phase=d.get("phase", "execute"),
            project_id=d.get("project_id", ""),
            issue_number=int(d.get("issue_number", 0) or 0),
            title=d.get("title", ""),
            body=d.get("body", ""),
            branch=d.get("branch", ""),
            instructions=d.get("instructions", ""),
            extra=d.get("extra", {}) or {},
        )


@dataclass
class AgentJobResult:
    """The result an Agent Execution Job writes to its output ConfigMap.

    The agent serializes one of these with :meth:`to_payload`; the worker rebuilds
    it with :meth:`from_payload`. ``job_name`` is assigned by the reader (the
    worker knows which Job it polled) and is not part of the wire payload."""

    status: str = JobStatus.FAILED.value
    job_name: str = ""
    issue_number: int = 0
    branch: str = ""
    pr_url: str = ""
    # number of commits the agent produced (execute/review phases)
    commits: int = 0
    tests_passed: bool = False
    # mid-run question (status == awaiting_human)
    question: str = ""
    # plan phase output (codebase-grounded plan from the planner Agent Job)
    plan: dict | None = None
    # review phase output
    review: dict | None = None
    # diagnosis phase output
    diagnosis: dict | None = None
    # merge / summarize output
    summary: str = ""
    merged_issues: list[int] = field(default_factory=list)
    merge_commit: str = ""
    error: str = ""

    def to_payload(self) -> dict:
        """Render the dict the agent stores under ``KEY_RESULT`` (drops the
        reader-assigned ``job_name``)."""
        d = asdict(self)
        d.pop("job_name", None)
        return d

    @classmethod
    def from_payload(cls, payload: dict, job_name: str) -> "AgentJobResult":
        """Rebuild an AgentJobResult from a Job's output payload (worker side)."""
        return cls(
            status=payload.get("status", JobStatus.FAILED.value),
            job_name=job_name,
            issue_number=int(payload.get("issue_number", 0) or 0),
            branch=payload.get("branch", ""),
            pr_url=payload.get("pr_url", ""),
            commits=int(payload.get("commits", 0) or 0),
            tests_passed=bool(payload.get("tests_passed", False)),
            question=payload.get("question", ""),
            plan=payload.get("plan"),
            review=payload.get("review"),
            diagnosis=payload.get("diagnosis"),
            summary=payload.get("summary", ""),
            merged_issues=list(payload.get("merged_issues", []) or []),
            merge_commit=payload.get("merge_commit", ""),
            error=payload.get("error", ""),
        )


@dataclass
class DispatchInput:
    project_id: str
    issue_number: int
    task_spec: TaskSpec
    # test override: poll interval / job ttl (seconds)
    poll_interval_seconds: float = 5.0
    retention_seconds: float = 300.0
    # For jobs not backed by a registry project (e.g. custom consumer workflows):
    # override the image / omneval ingest secret / repo without a registry entry.
    image_override: str = ""
    omneval_secret_override: str = ""
    github_url_override: str = ""
    # GitHub token Secret name; empty means the job needs no GitHub access
    github_token_secret_override: str = ""
    # ServiceAccount the Job pod runs as; empty falls back to the default SA
    service_account_override: str = ""


@dataclass
class OpenAgentPRsInput:
    """Input for the activity that lists issue numbers with an open agent PR."""

    project_id: str


@dataclass
class AnswerInput:
    job_name: str
    answer: str


@dataclass
class AwaitInput:
    """Resume polling a parked Job. Only the job name and poll cadence are needed
    — the poll reads neither project nor task spec."""

    job_name: str
    poll_interval_seconds: float = 5.0


@dataclass
class PollPRChecksInput:
    """Input for polling CI check runs on a draft PR."""

    project_id: str
    pr_number: int
    timeout_seconds: float = 300.0


# ---------------------------------------------------------------------------
# GitHub Issue comment notification activity I/O
# ---------------------------------------------------------------------------


@dataclass
class GithubNotificationInput:
    """Input for the post_github_comment activity.

    Posts a comment to a GitHub Issue using the devloop-bot PAT
    (GITHUB_TOKEN env var / project's github_token_secret).
    """

    issue_number: int
    project_id: str
    body: str


@dataclass
class PollCIChecksInput:
    """Input for the poll_ci_checks activity.

    Polls the GitHub Checks/Status APIs for the head commit of the given PR
    (``gh pr checks`` equivalent) and reports whether every check has passed,
    plus details on any that failed — consumed by ``Phase.CI_FIX`` so the fix
    Agent Execution Job knows exactly what to address.
    """

    project_id: str
    pr_number: int


@dataclass
class CICheckFailure:
    """One failing CI check, as surfaced to the ci_fix Agent Execution Job via
    ``TaskSpec.extra["ci_check_failures"]``."""

    name: str
    conclusion: str = ""
    details_url: str = ""
    summary: str = ""


@dataclass
class CIChecksResult:
    """The poll_ci_checks activity's result: pass/fail plus failure details.

    ``pending`` distinguishes "still running — wait and re-poll" from
    "genuinely failing — dispatch a fix": it's ``True`` only when no check
    has actually failed yet but at least one is still queued/in-progress (or
    the poll itself couldn't be completed, e.g. a transient GitHub-side
    error). ``all_passed`` can be ``False`` while ``pending`` is ``True`` and
    ``failures`` is empty — that combination means "not done yet", not "red"
    (issue #90).
    """

    all_passed: bool = False
    pending: bool = False
    failures: list[CICheckFailure] = field(default_factory=list)


@dataclass
class RequestReviewerInput:
    """Input for the request_github_reviewer activity.

    Requests a GitHub user as a reviewer on a PR.
    """

    project_id: str
    pr_number: int
    reviewer: str


@dataclass
class ReviewerRequestResult:
    """Outcome of the request_github_reviewer activity: whether a reviewer
    was actually requested, and — when not — why (no reviewer configured,
    no PR to request on, or a GitHub API failure). Lets callers like
    ``_notify_reviewer`` phrase their notification honestly instead of
    assuming the request succeeded (issue #88)."""

    requested: bool = False
    reason: str = ""


@dataclass
class GetPRBranchInput:
    """Input for the get_pr_branch activity.

    Resolves a PR's head branch name from its number — needed when the event
    that triggered ``PRCommentWorkflow`` didn't carry it. ``issue_comment``
    webhook payloads (an ``@devloop-bot`` mention in a PR conversation)
    reference the PR only by number; unlike ``pull_request_review`` payloads,
    they carry no ``pull_request.head.ref`` (issue #101).
    """

    project_id: str
    pr_number: int


@dataclass
class GetPRDiffInput:
    """Input for the get_pr_diff activity.

    Standalone activity kept for consumers that register it directly
    (``devloop.github_ops.get_pr_diff``) — devloop's own ``PRCommentWorkflow``
    no longer calls it (the agent fetches the diff itself via ``gh pr diff``,
    issue #98), but removing the symbol entirely broke downstream workers that
    import it for registration on their own task queues.
    """

    project_id: str
    pr_number: int


# ---------------------------------------------------------------------------
# Summarization delivery activity I/O (issue #79)
# ---------------------------------------------------------------------------


@dataclass
class PublishSummaryInput:
    """Input for the publish_summary activity.

    Carries the rendered digest from SummarizationWorkflow to the activity that
    delivers it: opens a GitHub Issue (label ``devloop-summary``, created if
    absent) and optionally POSTs the same payload to ``SUMMARIZATION_WEBHOOK_URL``
    as JSON (fire-and-forget).
    """

    project_id: str
    summary: str
    date: str  # ISO date string, e.g. "2026-06-06"


@dataclass
class CreateGithubIssueInput:
    """Input for the create_github_issue activity.

    Creates a new GitHub Issue with the given title, body, and labels.
    Returns the created issue number.
    """

    project_id: str
    title: str
    body: str
    labels: list[str]


@dataclass
class UpdateGithubIssueInput:
    """Input for the update_github_issue activity.

    Patches an existing GitHub Issue's body and/or state.
    Only non-empty fields are included in the PATCH payload.
    ``state`` accepts ``"closed"`` or ``""`` (no change).
    """

    project_id: str
    issue_number: int
    body: str = ""
    state: str = ""  # accepts "closed" or "" (no change)
