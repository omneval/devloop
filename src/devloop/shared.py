"""Sandbox-safe data structures shared between workflows and activities.

This module is imported by both Temporal workflow definitions (which run in
the deterministic sandbox) and activity code, so it must only import from the
standard library — no I/O, no threading, no network clients.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# Task queues
ORCHESTRATION_QUEUE = "devloop-orchestration"
DISCORD_QUEUE = "discord-bot"

# Discord channel logical names (resolved to IDs inside the bot)
CHANNEL_APPROVALS = "approvals"
CHANNEL_ALERTS = "alerts"
CHANNEL_CHANGELOG = "changelog"


class Phase(str, Enum):
    PLAN = "plan"
    EXECUTE = "execute"
    REVIEW = "review"
    MERGE = "merge"
    DIAGNOSIS = "diagnosis"
    REMEDIATION = "remediation"
    SUMMARIZE = "summarize"


class JobStatus(str, Enum):
    COMPLETE = "complete"
    FAILED = "failed"
    AWAITING_HUMAN = "awaiting_human"


class Verdict(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


# ---------------------------------------------------------------------------
# Discord activity I/O (mirror of images/discord-bot/activities.py dataclasses)
# ---------------------------------------------------------------------------


@dataclass
class SendMessageInput:
    workflow_id: str
    message: str
    channel: str = CHANNEL_APPROVALS
    thread_name: str = ""


@dataclass
class SendMessageOutput:
    thread_id: str


@dataclass
class SendNotificationInput:
    workflow_id: str
    message: str


@dataclass
class ArchiveThreadInput:
    workflow_id: str


# ---------------------------------------------------------------------------
# Planning / execution model
# ---------------------------------------------------------------------------


@dataclass
class PlannedIssue:
    number: int
    title: str
    body: str = ""
    depends_on: list[int] = field(default_factory=list)


@dataclass
class ExecutionPlan:
    project_id: str
    issues: list[PlannedIssue] = field(default_factory=list)

    def render(self) -> str:
        if not self.issues:
            return "_No open agent-ready issues to plan._"
        lines = [
            f"**Execution plan for `{self.project_id}`** ({len(self.issues)} issue(s)):",
            "",
        ]
        for i, issue in enumerate(self.issues, 1):
            dep = (
                f" (after #{', #'.join(map(str, issue.depends_on))})"
                if issue.depends_on
                else ""
            )
            lines.append(f"{i}. #{issue.number} — {issue.title}{dep}")
        lines += [
            "",
            "Reply **approve** to proceed, or reply with feedback to re-plan.",
        ]
        return "\n".join(lines)


@dataclass
class TaskSpec:
    """The instruction payload handed to an Agent Execution Job."""

    phase: str
    project_id: str
    issue_number: int = 0
    title: str = ""
    body: str = ""
    branch: str = ""
    instructions: str = ""
    # phase-specific extras (review rubric, merge branch list, alert payload …)
    extra: dict = field(default_factory=dict)


@dataclass
class AgentJobResult:
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
    job_name: str
    project_id: str
    issue_number: int
    task_spec: TaskSpec
    poll_interval_seconds: float = 5.0
