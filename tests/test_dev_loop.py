"""Dev Loop workflow tests (sequential model) using Temporal's time-skipping
env with mocked activities on the orchestration task queue."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from devloop import dev_loop_logic as logic
from devloop.dev_loop import DevLoopInput, DevLoopWorkflow
from devloop.shared import (
    ORCHESTRATION_QUEUE,
    AgentJobResult,
    GithubNotificationInput,
    JobStatus,
)


# --------------------------------------------------------------------------- #
# Configurable mock state
# --------------------------------------------------------------------------- #
@dataclass
class Mocks:
    # plan docs returned per plan dispatch; once exhausted, plan_default is used
    plan_rounds: list[dict] = field(default_factory=list)
    plan_default: dict = field(default_factory=lambda: {"issues": []})
    plan_calls: int = 0
    dispatch_behavior: dict = field(
        default_factory=dict
    )  # (phase, issue) -> AgentJobResult
    execute_commits: int = 1
    execute_status: str = JobStatus.COMPLETE.value
    review_commits: int = 1
    review_payload: dict | None = None  # AgentJobResult.review the review job returns
    merge_status: str = JobStatus.COMPLETE.value
    await_status: str = JobStatus.COMPLETE.value
    # recorders
    github_comments: list = field(default_factory=list)  # GithubNotificationInput records
    answers: list = field(default_factory=list)
    post_comments: list = field(default_factory=list)
    dispatched_phases: list = field(default_factory=list)
    # issue numbers the "open_agent_pr_issue_numbers" activity reports as already
    # having an open review PR (planner should skip these)
    open_agent_prs: list = field(default_factory=list)

    @property
    def notifications(self):
        """Compatibility shim: return all GitHub comment bodies."""
        return [c.body for c in self.github_comments]

    @property
    def messages(self):
        """Compatibility shim: return GitHub comment bodies (formerly Discord messages)."""
        return [c.body for c in self.github_comments]


M = Mocks()


def _one_issue(num=1):
    return {
        "issues": [
            {"id": str(num), "title": f"Issue {num}", "branch": f"agent/issue-{num}"}
        ]
    }


def _make_activities():
    @activity.defn(name="dispatch_agent_job")
    async def dispatch_agent_job(inp) -> AgentJobResult:
        phase = (
            inp["task_spec"]["phase"] if isinstance(inp, dict) else inp.task_spec.phase
        )
        issue = inp["issue_number"] if isinstance(inp, dict) else inp.issue_number
        M.dispatched_phases.append(phase)
        key = (phase, issue)
        if key in M.dispatch_behavior:
            return M.dispatch_behavior[key]
        if phase == "plan":
            doc = (
                M.plan_rounds[M.plan_calls]
                if M.plan_calls < len(M.plan_rounds)
                else M.plan_default
            )
            M.plan_calls += 1
            return AgentJobResult(
                status=JobStatus.COMPLETE.value, job_name="plan", plan=doc
            )
        if phase == "execute":
            if M.execute_status != JobStatus.COMPLETE.value:
                return AgentJobResult(
                    status=M.execute_status,
                    job_name=f"j{issue}",
                    issue_number=issue,
                    error="boom",
                )
            has = M.execute_commits > 0
            return AgentJobResult(
                status=JobStatus.COMPLETE.value,
                job_name=f"j{issue}",
                issue_number=issue,
                branch=f"agent/issue-{issue}" if has else "",
                pr_url=f"https://github.com/omneval/omneval/pull/{issue}"
                if has
                else "",
                commits=M.execute_commits,
                tests_passed=True,
            )
        if phase == "review":
            return AgentJobResult(
                status=JobStatus.COMPLETE.value,
                job_name=f"r{issue}",
                issue_number=issue,
                commits=M.review_commits,
                review=M.review_payload,
            )
        if phase == "merge":
            return AgentJobResult(
                status=M.merge_status,
                job_name="merge",
                pr_url=f"https://github.com/omneval/omneval/pull/{issue}",
                merged_issues=[issue],
                error="merge conflict"
                if M.merge_status != JobStatus.COMPLETE.value
                else "",
            )
        return AgentJobResult(status=JobStatus.COMPLETE.value, job_name="x")

    @activity.defn(name="answer_agent_job")
    async def answer_agent_job(inp) -> None:
        M.answers.append(inp["answer"] if isinstance(inp, dict) else inp.answer)

    @activity.defn(name="await_agent_job")
    async def await_agent_job(inp) -> AgentJobResult:
        # AwaitInput now carries only job_name + poll interval; the dispatch mock
        # names parked jobs "j<issue>", so recover the issue from the job name.
        job_name = inp["job_name"] if isinstance(inp, dict) else inp.job_name
        issue = int(job_name.removeprefix("j") or 0)
        if M.await_status != JobStatus.COMPLETE.value:
            return AgentJobResult(
                status=M.await_status,
                job_name=f"j{issue}",
                issue_number=issue,
                error="post-answer failure",
            )
        return AgentJobResult(
            status=JobStatus.COMPLETE.value,
            job_name=f"j{issue}",
            issue_number=issue,
            branch=f"agent/issue-{issue}",
            pr_url=f"https://github.com/omneval/omneval/pull/{issue}",
            commits=1,
            tests_passed=True,
        )

    @activity.defn(name="post_github_comment")
    async def post_github_comment(inp: GithubNotificationInput) -> None:
        M.github_comments.append(inp)

    @activity.defn(name="open_agent_pr_issue_numbers")
    async def open_agent_pr_issue_numbers(inp) -> list:
        return list(M.open_agent_prs)

    @activity.defn(name="post_pr_comments")
    async def post_pr_comments(inp) -> None:
        M.dispatched_phases.append("post_pr_comments")
        M.post_comments.append(inp)

    return [
        dispatch_agent_job,
        answer_agent_job,
        await_agent_job,
        open_agent_pr_issue_numbers,
        post_pr_comments,
        post_github_comment,
    ]


@pytest.fixture
def reset_mocks():
    global M
    M = Mocks()
    return M


async def _run_devloop(client: Client, inp: DevLoopInput, replies: list[str]):
    wf_id = f"devloop-test-{uuid.uuid4().hex[:8]}"
    handle = await client.start_workflow(
        DevLoopWorkflow.run, inp, id=wf_id, task_queue=ORCHESTRATION_QUEUE
    )
    for r in replies:
        await handle.signal(DevLoopWorkflow.human_reply, r)
    return await handle.result()


async def _env_and_run(inp: DevLoopInput, replies: list[str]):
    acts = _make_activities()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=ORCHESTRATION_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=acts,
        ):
            return await _run_devloop(env.client, inp, replies)


# --------------------------------------------------------------------------- #
# Pure rendering helpers
# --------------------------------------------------------------------------- #
def test_render_plan_names_next_issue_and_candidates():
    issues = [
        {"id": "1", "title": "First", "branch": "agent/issue-1"},
        {"id": "2", "title": "Second", "branch": "agent/issue-2"},
    ]
    text = logic.render_plan("omneval", 3, issues)
    assert "round 3" in text
    assert "#1 — First" in text and "agent/issue-1" in text
    assert "#2 — Second" in text  # listed as another candidate
    assert "approve" in text.lower()


def test_merge_gate_message_includes_pr():
    text = logic.merge_gate_message({"id": "7", "title": "Thing"}, "https://x/pull/7")
    assert "#7" in text and "https://x/pull/7" in text and "approve" in text.lower()


# --------------------------------------------------------------------------- #
# Plan phase + gate (#20)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_plan_skips_issue_with_open_review_pr(reset_mocks):
    """An issue that already has an open agent PR is dropped from the plan (it's
    awaiting human merge), so the loop doesn't re-work it. With it filtered out
    and no other issues, the loop completes without executing anything."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.open_agent_prs = [1]
    result = await _env_and_run(
        DevLoopInput("omneval", question_timeout_seconds=1),
        [],  # no gates reached — nothing to approve
    )
    assert result.status == "completed"
    assert result.merged_issues == []
    assert "plan" in M.dispatched_phases
    assert "execute" not in M.dispatched_phases
    assert any("skipping" in n.lower() and "#1" in n for n in M.notifications)


@pytest.mark.asyncio
async def test_plan_approve_runs_one_issue_to_merge(reset_mocks):
    reset_mocks.plan_rounds = [_one_issue(1)]  # round 1; round 2 plan is empty
    result = await _env_and_run(
        DevLoopInput("omneval", question_timeout_seconds=1),
        ["approve", "approve"],  # plan gate, merge gate
    )
    assert result.status == "completed"
    assert result.merged_issues == [1]
    assert M.dispatched_phases == ["plan", "execute", "review", "merge", "plan"]


@pytest.mark.asyncio
async def test_plan_replan_then_approve(reset_mocks):
    # First plan offers #1 and #2; reviewer says drop #2; re-plan offers only #1.
    reset_mocks.plan_rounds = [
        {
            "issues": [
                {"id": "1", "title": "A", "branch": "agent/issue-1"},
                {"id": "2", "title": "B", "branch": "agent/issue-2"},
            ]
        },
        _one_issue(1),
    ]
    result = await _env_and_run(
        DevLoopInput("omneval", question_timeout_seconds=1),
        ["please drop #2", "approve", "approve"],
    )
    assert result.status == "completed"
    assert result.merged_issues == [1]
    assert M.plan_calls == 3  # 2 in round 1 (reject+approve) + 1 empty round 2


@pytest.mark.asyncio
async def test_plan_replan_exhaustion_fails(reset_mocks):
    reset_mocks.plan_default = _one_issue(1)  # planner always offers an issue
    result = await _env_and_run(
        DevLoopInput("omneval", replan_max=2),
        ["no", "still no", "nope"],  # 3 rejections > replan_max(2)
    )
    assert result.status == "failed_plan"
    assert any("rejected" in n.lower() for n in M.notifications)


# --------------------------------------------------------------------------- #
# Execute phase (#21)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_execute_no_commits_skips_to_next_round(reset_mocks):
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.execute_commits = 0
    result = await _env_and_run(DevLoopInput("omneval"), ["approve"])
    assert result.status == "completed"
    assert result.merged_issues == []
    assert "review" not in M.dispatched_phases and "merge" not in M.dispatched_phases
    assert any("no commits" in n.lower() for n in M.notifications)


@pytest.mark.asyncio
async def test_execute_mid_run_question_reply(reset_mocks):
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.dispatch_behavior[("execute", 1)] = AgentJobResult(
        status=JobStatus.AWAITING_HUMAN.value,
        job_name="j1",
        issue_number=1,
        question="Use lib A or B?",
    )
    result = await _env_and_run(
        DevLoopInput("omneval", question_timeout_seconds=60),
        ["approve", "use lib A", "approve"],
    )
    assert result.status == "completed"
    assert result.merged_issues == [1]
    assert M.answers == ["use lib A"]
    assert any("Use lib A or B?" in m for m in M.messages)


@pytest.mark.asyncio
async def test_execute_mid_run_question_timeout(reset_mocks):
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.dispatch_behavior[("execute", 1)] = AgentJobResult(
        status=JobStatus.AWAITING_HUMAN.value,
        job_name="j1",
        issue_number=1,
        question="Which approach?",
    )
    reset_mocks.await_status = JobStatus.FAILED.value  # best-guess answer then fails
    result = await _env_and_run(
        DevLoopInput("omneval", question_timeout_seconds=1),
        ["approve"],  # plan approval only; mid-run question times out
    )
    assert result.status == "completed"
    assert M.answers and "best guess" in M.answers[0].lower()
    assert any("best-guess" in n.lower() for n in M.notifications)


# --------------------------------------------------------------------------- #
# Merge gate + Merge (#23)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_merge_gate_skip(reset_mocks):
    reset_mocks.plan_rounds = [_one_issue(1)]
    result = await _env_and_run(DevLoopInput("omneval"), ["approve", "no"])
    assert result.status == "completed"
    assert result.merged_issues == []
    assert any("not approved for merge" in n.lower() for n in M.notifications)


@pytest.mark.asyncio
async def test_merge_gate_timeout_leaves_pr_open_and_moves_on(reset_mocks):
    """No merge decision within gate_timeout_seconds: the PR is left open, the
    issue is not merged, and the loop moves on (round 2 plan is empty → done).
    The merge agent Job is never dispatched."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    result = await _env_and_run(
        DevLoopInput("omneval", gate_timeout_seconds=1),
        ["approve"],  # plan gate approved; merge gate gets no reply → times out
    )
    assert result.status == "completed"
    assert result.merged_issues == []
    assert "merge" not in M.dispatched_phases  # never dispatched the merge job
    assert any(
        "timed out" in n.lower() and "moving on" in n.lower() for n in M.notifications
    )


@pytest.mark.asyncio
async def test_plan_gate_timeout_pauses(reset_mocks):
    """No approval within gate_timeout_seconds at the plan gate pauses the loop
    (status 'paused') without running any unreviewed work."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    result = await _env_and_run(
        DevLoopInput("omneval", gate_timeout_seconds=1),
        [],  # nobody approves the plan
    )
    assert result.status == "paused"
    assert result.merged_issues == []
    assert "execute" not in M.dispatched_phases
    assert any("plan gate timed out" in n.lower() for n in M.notifications)


def test_from_env_reads_timeout_overrides(monkeypatch):
    """The webhook/schedule entry points build the input via from_env, which
    sources the gate/question timeouts from the worker environment (wired by the
    Helm chart) and leaves other fields at their dataclass defaults."""
    monkeypatch.setenv("GATE_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("QUESTION_TIMEOUT_SECONDS", "900")
    inp = DevLoopInput.from_env("omneval", "agent-ready")
    assert inp.project_id == "omneval"
    assert inp.agent_label == "agent-ready"
    assert inp.gate_timeout_seconds == 600.0
    assert inp.question_timeout_seconds == 900.0


def test_from_env_falls_back_to_defaults(monkeypatch):
    """Missing or malformed env values fall back to the dataclass defaults rather
    than crashing the webhook/schedule path."""
    monkeypatch.delenv("GATE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("QUESTION_TIMEOUT_SECONDS", "not-a-number")
    inp = DevLoopInput.from_env("omneval")
    assert inp.gate_timeout_seconds == DevLoopInput.gate_timeout_seconds == 14400.0
    assert inp.question_timeout_seconds == DevLoopInput.question_timeout_seconds


@pytest.mark.asyncio
async def test_merge_failure_terminates(reset_mocks):
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.merge_status = JobStatus.FAILED.value
    result = await _env_and_run(DevLoopInput("omneval"), ["approve", "approve"])
    assert result.status == "failed_merge"
    assert any("parked" in n.lower() for n in M.notifications)


# --------------------------------------------------------------------------- #
# Review phase posts findings to the PR (#22)
# --------------------------------------------------------------------------- #
def _field(obj, name):
    return obj[name] if isinstance(obj, dict) else getattr(obj, name)


@pytest.mark.asyncio
async def test_review_posts_findings_to_pr(reset_mocks):
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.review_payload = {
        "summary": "looks good, tightened error handling",
        "inline_comments": [{"file": "a.py", "line": 3, "body": "nit"}],
    }
    result = await _env_and_run(DevLoopInput("omneval"), ["approve", "approve"])
    assert result.status == "completed"
    assert "post_pr_comments" in M.dispatched_phases
    posted = M.post_comments[0]
    assert "tightened error handling" in _field(posted, "summary")
    # PR number parsed from the execute phase's pr_url (…/pull/1)
    assert _field(posted, "pr_number") == 1


@pytest.mark.asyncio
async def test_review_no_findings_skips_post(reset_mocks):
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.review_payload = None  # reviewer returned no structured findings
    result = await _env_and_run(DevLoopInput("omneval"), ["approve", "approve"])
    assert result.status == "completed"
    assert "post_pr_comments" not in M.dispatched_phases
