"""Execution model types for workflow orchestration and workers.

Contains the dataclasses used by the worker to dispatch Agent Execution Jobs
and the agent entrypoint to read task specs and report results — essentially
the instruction/result payload that connects the Temporal workflow to the
Kubernetes Job lifecycle.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass
class TaskSpec:
    """The instruction payload handed to an Agent Execution Job.

    Serialized into the Job's ``TASK_SPEC`` env var by the worker and rebuilt by
    the agent entrypoint — both via the methods below, so the field set has one
    owner.
    """

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

    status: str = "failed"
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
            status=payload.get("status", "failed"),
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


@dataclass
class WorkflowKpiInput:
    """Input for the emit_workflow_kpis activity (issue #122).

    One emission per issue the Dev Loop carried to reviewer notification.
    All counters are absolute for that issue's run; ``label_to_pr_seconds``
    is the wall-clock from workflow start (≈ the ``agent-ready`` labeling,
    since the webhook is the sole entry point) to the reviewer hand-off.
    """

    project_id: str
    issue_number: int = 0
    ci_fix_iterations: int = 0
    review_fix_passes: int = 0
    answer_jobs: int = 0
    execute_attempts: int = 0
    review_verdict: str = ""
    label_to_pr_seconds: float = 0.0
    pr_opened: bool = False
    commits: int = 0
    ci_exhausted: bool = False