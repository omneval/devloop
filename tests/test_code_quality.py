"""Tests for CodeQualityWorkflow and related schedule/worker integration.

Covers the three workflow paths (pass, fail, abort) plus schedule management.
Uses the same WorkflowEnvironment + fake-activity pattern established in
test_summarization.py and the fake-client pattern from test_schedules.py.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest
from temporalio import activity
from temporalio.client import (
    Schedule,
    ScheduleAlreadyRunningError,
)
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from devloop.code_quality import CodeQualityInput, CodeQualityWorkflow
from devloop.projects import ProjectConfig
from devloop.shared import (
    AgentJobResult,
    CreateGithubIssueInput,
    DispatchInput,
    GithubNotificationInput,
    JobStatus,
    JOB_DISPATCH_QUEUE,
    ORCHESTRATION_QUEUE,
    Phase,
    UpdateGithubIssueInput,
)
from devloop import schedules


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project(project_id: str = "myrepo") -> ProjectConfig:
    return ProjectConfig(
        id=project_id,
        github_url=f"https://github.com/example/{project_id}",
        default_branch="main",
        agent_image="ghcr.io/example/agent:sha-abc",
        agent_label="agent-ready",
        omneval_ingest_secret="omneval-ingest",
        github_token_secret="agent-github-token",
    )


# ---------------------------------------------------------------------------
# Fake-client for schedule tests (mirrors test_schedules.py pattern)
# ---------------------------------------------------------------------------


class _FakeHandle:
    def __init__(self, raises: Exception | None = None):
        self._raises = raises
        self.deleted = False

    async def update(self, updater, **kwargs):
        if self._raises is not None:
            raise self._raises

    async def delete(self):
        self.deleted = True


class _FakeClient:
    def __init__(self, *, already_running: bool = False):
        self._already_running = already_running
        self.created: list[tuple[str, Schedule]] = []
        self.handles: dict[str, _FakeHandle] = {}

    async def create_schedule(self, schedule_id, schedule, **kwargs):
        if self._already_running:
            raise ScheduleAlreadyRunningError()
        self.created.append((schedule_id, schedule))
        return schedule_id

    def get_schedule_handle(self, schedule_id):
        handle = _FakeHandle()
        self.handles[schedule_id] = handle
        return handle


# ---------------------------------------------------------------------------
# Fake activities for workflow tests
# ---------------------------------------------------------------------------


@dataclass
class _WorkflowCalls:
    """Captures all activity calls made during a workflow run."""

    issues_created: list[CreateGithubIssueInput] = field(default_factory=list)
    issues_updated: list[UpdateGithubIssueInput] = field(default_factory=list)
    comments_posted: list[dict] = field(default_factory=list)
    dispatches: list[dict] = field(default_factory=list)


def _make_activities(calls: _WorkflowCalls, scan_plan: dict, improve_summary: str = ""):
    """Build fake activity implementations that record calls and return canned data."""

    @activity.defn(name="create_github_issue")
    async def create_github_issue(inp: CreateGithubIssueInput) -> int:
        calls.issues_created.append(inp)
        return 42  # fixed issue number for all tests

    @activity.defn(name="update_github_issue")
    async def update_github_issue(inp: UpdateGithubIssueInput) -> None:
        calls.issues_updated.append(inp)

    @activity.defn(name="post_github_comment")
    async def post_github_comment(inp: GithubNotificationInput) -> None:
        calls.comments_posted.append(
            {"issue_number": inp.issue_number, "body": inp.body}
        )

    @activity.defn(name="dispatch_agent_job")
    async def dispatch_agent_job(inp: DispatchInput) -> AgentJobResult:
        phase = (
            inp.task_spec.phase
            if not isinstance(inp.task_spec, dict)
            else inp.task_spec["phase"]
        )
        calls.dispatches.append({"task_spec": inp.task_spec})
        if phase == Phase.CODE_QUALITY_SCAN.value:
            return AgentJobResult(
                status=JobStatus.COMPLETE.value,
                plan=scan_plan,
                summary="scan done",
            )
        # improve phase
        return AgentJobResult(
            status=JobStatus.COMPLETE.value,
            summary=improve_summary or "filed 3 issues",
        )

    return [
        create_github_issue,
        update_github_issue,
        post_github_comment,
        dispatch_agent_job,
    ]


async def _run_workflow(
    inp: CodeQualityInput,
    calls: _WorkflowCalls,
    scan_plan: dict,
    improve_summary: str = "",
):
    """Run CodeQualityWorkflow with fake activities in a time-skipping env."""
    acts = _make_activities(calls, scan_plan, improve_summary)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=ORCHESTRATION_QUEUE,
            workflows=[CodeQualityWorkflow],
            activities=acts,
        ):
            async with Worker(
                env.client,
                task_queue=JOB_DISPATCH_QUEUE,
                workflows=[],
                activities=acts,
            ):
                await env.client.execute_workflow(
                    CodeQualityWorkflow.run,
                    inp,
                    id=f"cq-{uuid.uuid4().hex[:8]}",
                    task_queue=ORCHESTRATION_QUEUE,
                )


# ---------------------------------------------------------------------------
# Cycle 1: Pass path — score >= threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pass_path_closes_issue_with_check_comment():
    """When score >= threshold the parent issue is closed and a ✅ comment posted."""
    calls = _WorkflowCalls()
    scan_plan = {
        "score": 8000,
        "report": "all good",
        "scan_error": False,
        "error_message": "",
    }
    inp = CodeQualityInput(project_id="myrepo", threshold=7000)

    await _run_workflow(inp, calls, scan_plan)

    # Parent issue created
    assert len(calls.issues_created) == 1
    assert calls.issues_created[0].project_id == "myrepo"
    assert "devloop-code-quality" in calls.issues_created[0].labels

    # Issue eventually closed
    close_calls = [u for u in calls.issues_updated if u.state == "closed"]
    assert len(close_calls) >= 1

    # ✅ comment posted
    check_comments = [c for c in calls.comments_posted if "✅" in c["body"]]
    assert len(check_comments) >= 1


# ---------------------------------------------------------------------------
# Cycle 2: Abort path — scan_error = True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_abort_path_closes_issue_without_improve_dispatch():
    """When scan_error=True the issue is closed with an error body and improve is NOT dispatched."""
    calls = _WorkflowCalls()
    scan_plan = {
        "score": 0,
        "report": "",
        "scan_error": True,
        "error_message": "no rules.toml found",
    }
    inp = CodeQualityInput(project_id="myrepo", threshold=7000)

    await _run_workflow(inp, calls, scan_plan)

    # Issue closed
    close_calls = [u for u in calls.issues_updated if u.state == "closed"]
    assert len(close_calls) >= 1

    # No improve phase dispatched
    improve_dispatches = [
        d
        for d in calls.dispatches
        if d["task_spec"].phase == Phase.CODE_QUALITY_IMPROVE.value
    ]
    assert improve_dispatches == []


# ---------------------------------------------------------------------------
# Cycle 3: Fail path — score < threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_path_dispatches_improve_and_posts_completion_comment():
    """When score < threshold the improve phase is dispatched and a 📋 comment posted."""
    calls = _WorkflowCalls()
    scan_plan = {
        "score": 5000,
        "report": "many issues",
        "scan_error": False,
        "error_message": "",
    }
    inp = CodeQualityInput(project_id="myrepo", threshold=7000)

    await _run_workflow(inp, calls, scan_plan, improve_summary="filed 5 issues")

    # Improve phase dispatched
    improve_dispatches = [
        d
        for d in calls.dispatches
        if d["task_spec"].phase == Phase.CODE_QUALITY_IMPROVE.value
    ]
    assert len(improve_dispatches) == 1

    # ⚠️ comment before improve
    warn_comments = [c for c in calls.comments_posted if "⚠️" in c["body"]]
    assert len(warn_comments) >= 1

    # 📋 completion comment after improve
    done_comments = [c for c in calls.comments_posted if "📋" in c["body"]]
    assert len(done_comments) >= 1


# ---------------------------------------------------------------------------
# Cycle 4: ensure_schedules creates code-quality schedule when enabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_schedules_creates_code_quality_when_enabled():
    """When code_quality_enabled=True a code-quality-{project_id} schedule is created."""
    client = _FakeClient(already_running=False)

    await schedules.ensure_schedules(
        client,
        [_project("myrepo")],
        code_quality_enabled=True,
        code_quality_cron_schedule="0 9 * * 1",
    )

    cq_ids = [sid for sid, _ in client.created if sid.startswith("code-quality-")]
    assert "code-quality-myrepo" in cq_ids


# ---------------------------------------------------------------------------
# Cycle 5: ensure_schedules deletes code-quality schedule when disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_schedules_deletes_code_quality_when_disabled():
    """When code_quality_enabled=False any existing code-quality-{project_id} schedule is deleted."""
    client = _FakeClient(already_running=False)

    await schedules.ensure_schedules(
        client,
        [_project("myrepo")],
        code_quality_enabled=False,
    )

    # The handle for the code-quality schedule should have been obtained (for deletion)
    assert "code-quality-myrepo" in client.handles
    assert client.handles["code-quality-myrepo"].deleted is True
