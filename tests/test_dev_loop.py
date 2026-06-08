"""Dev Loop workflow tests (sequential model) using Temporal's time-skipping
env with mocked activities on the orchestration task queue."""

from __future__ import annotations

import uuid
from dataclasses import asdict as dataclasses_asdict, dataclass, field

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from devloop import dev_loop_logic as logic
from devloop.dev_loop import DevLoopInput, DevLoopWorkflow
from devloop.shared import (
    JOB_DISPATCH_QUEUE,
    ORCHESTRATION_QUEUE,
    AgentJobResult,
    CICheckFailure,
    CIChecksResult,
    GithubNotificationInput,
    JobStatus,
    ReviewerRequestResult,
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
    # when set, overrides execute_commits with a per-attempt sequence (one
    # entry consumed per "execute" dispatch for a given issue; last repeats)
    execute_commits_seq: list | None = None
    execute_status: str = JobStatus.COMPLETE.value
    review_commits: int = 1
    review_payload: dict | None = None  # AgentJobResult.review the review job returns
    await_status: str = JobStatus.COMPLETE.value
    # recorders
    github_comments: list = field(
        default_factory=list
    )  # GithubNotificationInput records
    answers: list = field(default_factory=list)
    post_comments: list = field(default_factory=list)
    dispatched_phases: list = field(default_factory=list)
    dispatched_specs: list = field(default_factory=list)  # recorded TaskSpec dicts
    # issue numbers the "open_agent_pr_issue_numbers" activity reports as already
    # having an open review PR (planner should skip these)
    open_agent_prs: list = field(default_factory=list)
    # reviewer requests recorded by request_github_reviewer mock
    reviewer_requests: list = field(default_factory=list)
    # result the request_github_reviewer mock returns (issue #88)
    reviewer_result: ReviewerRequestResult = field(
        default_factory=lambda: ReviewerRequestResult(requested=True)
    )
    # CI poll results returned in order; once exhausted, the last entry repeats.
    # Each entry: CIChecksResult(all_passed=..., failures=[...])
    ci_poll_results: list = field(default_factory=list)
    ci_poll_calls: int = 0
    ci_polls: list = field(default_factory=list)  # recorded PollCIChecksInput
    # ci_fix dispatch behavior: number of commits per attempt (cycles if shorter)
    ci_fix_commits: list = field(default_factory=lambda: [1])
    ci_fix_status: str = JobStatus.COMPLETE.value
    # Phase.ANSWER dispatch behavior: summary returned as the answer, and status
    answer_job_summary: str = "use lib A"
    answer_job_status: str = JobStatus.COMPLETE.value
    # remediation phase
    poll_pr_checks_result: dict = field(default_factory=lambda: {"failures": []})
    remediation_commits: int = 1
    remediation_status: str = JobStatus.COMPLETE.value

    @property
    def notifications(self):
        """Compatibility shim: return all GitHub comment bodies."""
        return [c.body for c in self.github_comments]


M = Mocks()


def _one_issue(num=1):
    return {
        "issues": [
            {"id": str(num), "title": f"Issue {num}", "branch": f"agent/issue-{num}"}
        ]
    }


def _make_activities():
    @activity.defn(name="poll_pr_checks")
    async def poll_pr_checks(inp):
        return M.poll_pr_checks_result

    @activity.defn(name="dispatch_agent_job")
    async def dispatch_agent_job(inp) -> AgentJobResult:
        spec = inp["task_spec"] if isinstance(inp, dict) else inp.task_spec
        phase = spec["phase"] if isinstance(spec, dict) else spec.phase
        issue = inp["issue_number"] if isinstance(inp, dict) else inp.issue_number
        M.dispatched_phases.append(phase)
        M.dispatched_specs.append(
            spec if isinstance(spec, dict) else dataclasses_asdict(spec)
        )
        key = (phase, issue)
        if key in M.dispatch_behavior:
            return M.dispatch_behavior[key]
        if phase == "remediation":
            return AgentJobResult(
                status=M.remediation_status,
                job_name=f"remediation-{issue}",
                issue_number=issue,
                commits=M.remediation_commits,
                branch=inp["task_spec"]["branch"]
                if isinstance(inp, dict)
                else inp.task_spec.branch,
                pr_url=f"https://github.com/example/test-project/pull/{issue}",
            )
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
            if M.execute_commits_seq is not None:
                attempt = M.dispatched_phases.count("execute") - 1
                seq = M.execute_commits_seq or [0]
                commits = seq[min(attempt, len(seq) - 1)]
            else:
                commits = M.execute_commits
            has = commits > 0
            return AgentJobResult(
                status=JobStatus.COMPLETE.value,
                job_name=f"j{issue}",
                issue_number=issue,
                branch=f"agent/issue-{issue}" if has else "",
                pr_url=f"https://github.com/omneval/omneval/pull/{issue}"
                if has
                else "",
                commits=commits,
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
        if phase == "answer":
            return AgentJobResult(
                status=M.answer_job_status,
                job_name=f"a{issue}",
                issue_number=issue,
                summary=M.answer_job_summary,
                error="" if M.answer_job_status == JobStatus.COMPLETE.value else "boom",
            )
        if phase == "ci_fix":
            attempt = M.dispatched_phases.count("ci_fix") - 1
            commits_seq = M.ci_fix_commits or [1]
            commits = commits_seq[min(attempt, len(commits_seq) - 1)]
            return AgentJobResult(
                status=M.ci_fix_status,
                job_name=f"cf{issue}",
                issue_number=issue,
                commits=commits,
                error="" if M.ci_fix_status == JobStatus.COMPLETE.value else "boom",
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

    @activity.defn(name="request_github_reviewer")
    async def request_github_reviewer(inp) -> ReviewerRequestResult:
        M.reviewer_requests.append(inp)
        return M.reviewer_result

    @activity.defn(name="poll_ci_checks")
    async def poll_ci_checks(inp) -> CIChecksResult:
        M.ci_polls.append(inp)
        results = M.ci_poll_results
        if not results:
            return CIChecksResult(all_passed=True, failures=[])
        idx = min(M.ci_poll_calls, len(results) - 1)
        M.ci_poll_calls += 1
        return results[idx]

    # dispatch_agent_job is dispatched on JOB_DISPATCH_QUEUE (issue #73); the
    # rest stay on ORCHESTRATION_QUEUE. Returned as two lists so the test
    # harness can register each with the Worker polling its queue.
    return {
        "dispatch": [dispatch_agent_job],
        "orchestration": [
            poll_pr_checks,
            answer_agent_job,
            await_agent_job,
            open_agent_pr_issue_numbers,
            post_pr_comments,
            post_github_comment,
            request_github_reviewer,
            poll_ci_checks,
        ],
    }


@pytest.fixture
def reset_mocks():
    global M
    M = Mocks()
    return M


async def _run_devloop(client: Client, inp: DevLoopInput):
    wf_id = f"devloop-test-{uuid.uuid4().hex[:8]}"
    handle = await client.start_workflow(
        DevLoopWorkflow.run, inp, id=wf_id, task_queue=ORCHESTRATION_QUEUE
    )
    return await handle.result()


async def _env_and_run(inp: DevLoopInput, replies: list[str] | None = None):
    """Run the workflow to completion. ``replies`` is accepted (and ignored)
    for backwards compatibility with older call sites — the human-reply loop
    has been replaced by Phase.ANSWER agent jobs (#77)."""
    acts = _make_activities()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with (
            Worker(
                env.client,
                task_queue=ORCHESTRATION_QUEUE,
                workflows=[DevLoopWorkflow],
                activities=acts["orchestration"],
            ),
            Worker(
                env.client,
                task_queue=JOB_DISPATCH_QUEUE,
                workflows=[],
                activities=acts["dispatch"],
            ),
        ):
            return await _run_devloop(env.client, inp)


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


# --------------------------------------------------------------------------- #
# Plan phase — no gate, runs directly (#74)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_plan_skips_issue_with_open_review_pr(reset_mocks):
    """An issue that already has an open agent PR is dropped from the plan (it's
    awaiting human merge), so the loop doesn't re-work it. With it filtered out
    and no other issues, the loop completes without executing anything."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.open_agent_prs = [1]
    result = await _env_and_run(
        DevLoopInput("omneval"),
        [],  # no gates — runs autonomously
    )
    assert result.status == "completed"
    assert result.queued_for_review == []
    assert "plan" in M.dispatched_phases
    assert "execute" not in M.dispatched_phases
    assert any("skipping" in n.lower() and "#1" in n for n in M.notifications)


@pytest.mark.asyncio
async def test_plan_phase_scopes_to_triggering_issue(reset_mocks):
    """The Plan TaskSpec must carry the triggering issue's number so the Plan
    agent scopes its work to that single issue rather than replanning the
    whole agent-ready backlog (which let the agent pick a different — and
    possibly much larger — issue to execute first, surprising whoever applied
    the agent_label to trigger this run; caught in real-cluster E2E testing)."""
    reset_mocks.plan_rounds = [_one_issue(7)]
    await _env_and_run(DevLoopInput("omneval", triggering_issue=7), [])

    plan_specs = [
        s for p, s in zip(M.dispatched_phases, M.dispatched_specs) if p == "plan"
    ]
    assert plan_specs
    assert plan_specs[0]["issue_number"] == 7


@pytest.mark.asyncio
async def test_autonomous_round_plan_execute_review_notify(reset_mocks):
    """Full autonomous round: plan → execute → review → reviewer notification.
    No human gates, no human replies needed. Result has queued_for_review."""
    reset_mocks.plan_rounds = [_one_issue(1)]  # round 1; round 2 plan is empty
    result = await _env_and_run(
        DevLoopInput("omneval"),
        [],  # no replies needed — fully autonomous
    )
    assert result.status == "completed"
    assert result.queued_for_review == [1]
    # plan then execute then review — no merge phase
    assert M.dispatched_phases[:3] == ["plan", "execute", "review"]
    assert "merge" not in M.dispatched_phases
    # reviewer was requested
    assert len(M.reviewer_requests) >= 1
    # notification comment posted about reviewer
    assert any("ready for review" in n.lower() for n in M.notifications)


@pytest.mark.asyncio
async def test_plan_returns_empty_on_no_issues(reset_mocks):
    """When the planner returns no issues, the loop completes immediately."""
    result = await _env_and_run(
        DevLoopInput("omneval"),
        [],
    )
    assert result.status == "completed"
    assert result.queued_for_review == []


# --------------------------------------------------------------------------- #
# Execute phase (#21)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_execute_no_commits_skips_to_next_round(reset_mocks):
    """With execute_max_iterations=1 (default) a zero-commit result exhausts
    immediately, posts the "exhausted" comment, and the round continues."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.execute_commits = 0
    result = await _env_and_run(DevLoopInput("omneval"), [])
    assert result.status == "completed"
    assert result.queued_for_review == []
    assert "review" not in M.dispatched_phases and "merge" not in M.dispatched_phases
    assert M.dispatched_phases.count("execute") == 1
    assert any(
        "exhausted 1 attempts with no commits" in n.lower() for n in M.notifications
    )


@pytest.mark.asyncio
async def test_execute_retries_zero_commit_result_then_parks(reset_mocks):
    """Zero commits on every attempt — the dispatch is retried up to
    execute_max_iterations times, each preceded by a "queued" comment, then the
    issue is parked with the "exhausted" comment and the round continues."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.execute_commits_seq = [0, 0, 0]
    result = await _env_and_run(
        DevLoopInput("omneval", execute_max_iterations=3),
        [],
    )
    assert result.status == "completed"
    assert result.queued_for_review == []
    assert "review" not in M.dispatched_phases and "merge" not in M.dispatched_phases
    assert M.dispatched_phases.count("execute") == 3
    queued = [
        n
        for n in M.notifications
        if "queued" in n.lower() and "working on this issue" in n.lower()
    ]
    assert len(queued) == 3
    assert any(
        "❌ execute exhausted 3 attempts with no commits — skipping this round"
        in n.lower()
        for n in M.notifications
    )


@pytest.mark.asyncio
async def test_execute_retry_succeeds_before_exhausting(reset_mocks):
    """A later retry produces commits — the loop stops retrying and proceeds
    normally into review (not parked, no "exhausted" comment)."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.execute_commits_seq = [0, 0, 2]
    result = await _env_and_run(
        DevLoopInput("omneval", execute_max_iterations=5),
        [],
    )
    assert result.status == "completed"
    assert result.queued_for_review == [1]
    assert M.dispatched_phases.count("execute") == 3
    assert "review" in M.dispatched_phases
    assert not any("exhausted" in n.lower() for n in M.notifications)
    assert any("✅ implemented" in n.lower() for n in M.notifications)


@pytest.mark.asyncio
async def test_execute_mid_run_question_spawns_answer_job(reset_mocks):
    """AWAITING_HUMAN dispatches a Phase.ANSWER job (question + branch in the
    TaskSpec); its summary is patched back as the answer and the original job
    resumes via await_agent_job."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.dispatch_behavior[("execute", 1)] = AgentJobResult(
        status=JobStatus.AWAITING_HUMAN.value,
        job_name="j1",
        issue_number=1,
        question="Use lib A or B?",
        branch="agent/issue-1",
    )
    reset_mocks.answer_job_summary = "Use lib A — it matches existing conventions."
    result = await _env_and_run(DevLoopInput("omneval"))

    assert result.status == "completed"
    assert result.queued_for_review == [1]

    # A Phase.ANSWER job was dispatched on JOB_DISPATCH_QUEUE with question/branch
    answer_specs = [s for s in M.dispatched_specs if s.get("phase") == "answer"]
    assert len(answer_specs) == 1
    assert answer_specs[0]["extra"].get("question") == "Use lib A or B?"
    assert answer_specs[0]["branch"] == "agent/issue-1"

    # the answer job's summary was patched back as the answer
    assert M.answers == ["Use lib A — it matches existing conventions."]

    # comments: queued before dispatch, and a record after completion
    assert any(
        "queued" in n.lower() and "answering agent question" in n.lower()
        for n in M.notifications
    )
    assert any(
        "agent asked" in n.lower()
        and "use lib a or b?" in n.lower()
        and "auto-answered by agent" in n.lower()
        and "use lib a — it matches existing conventions." in n.lower()
        for n in M.notifications
    )


@pytest.mark.asyncio
async def test_question_limit_reached_proceeds_with_best_guess(reset_mocks):
    """Once the phase run hits max_questions_per_phase questions, the workflow
    stops spawning answer jobs, answers with "proceed with your best guess",
    and posts the question-limit-reached comment."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    awaiting = AgentJobResult(
        status=JobStatus.AWAITING_HUMAN.value,
        job_name="j1",
        issue_number=1,
        question="Which approach?",
        branch="agent/issue-1",
    )
    reset_mocks.dispatch_behavior[("execute", 1)] = awaiting
    # Force the cap to trigger on the very first question — no answer job is
    # dispatched, the best-guess is patched directly, and await_agent_job
    # resumes the original job to completion.
    result = await _env_and_run(DevLoopInput("omneval", max_questions_per_phase=0))

    assert result.status == "completed"
    # No Phase.ANSWER job dispatched once the cap is reached
    assert not [s for s in M.dispatched_specs if s.get("phase") == "answer"]
    # best-guess answer patched back directly
    assert M.answers and "best guess" in M.answers[-1].lower()
    assert any(
        "question limit reached" in n.lower()
        and "which approach?" in n.lower()
        and "best guess" in n.lower()
        for n in M.notifications
    )


# --------------------------------------------------------------------------- #
# DevLoopResult shape (#74)
# --------------------------------------------------------------------------- #
def test_devloop_result_has_queued_for_review():
    """DevLoopResult must have queued_for_review, not merged_issues."""
    from devloop.dev_loop import DevLoopResult

    r = DevLoopResult(status="completed", queued_for_review=[1, 2])
    assert r.queued_for_review == [1, 2]
    assert not hasattr(r, "merged_issues")


def test_devloop_result_status_values():
    """Only 'completed' and 'failed_plan' are valid statuses."""
    from devloop.dev_loop import DevLoopResult

    # These should be constructable without error
    DevLoopResult(status="completed")
    DevLoopResult(status="failed_plan")
    # 'paused' and 'failed_merge' are removed


def test_devloop_input_no_gate_timeout_or_replan_max():
    """DevLoopInput must not have gate_timeout_seconds or replan_max fields."""
    import dataclasses
    from devloop.dev_loop import DevLoopInput

    field_names = {f.name for f in dataclasses.fields(DevLoopInput)}
    assert "gate_timeout_seconds" not in field_names
    assert "replan_max" not in field_names


def test_from_env_no_gate_timeout(monkeypatch):
    """from_env should not read GATE_TIMEOUT_SECONDS."""
    monkeypatch.setenv("GATE_TIMEOUT_SECONDS", "600")
    inp = DevLoopInput.from_env("omneval", "agent-ready")
    assert inp.project_id == "omneval"
    assert inp.agent_label == "agent-ready"
    # No gate_timeout_seconds field
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(DevLoopInput)}
    assert "gate_timeout_seconds" not in field_names


def test_devloop_input_no_question_timeout_seconds():
    """question_timeout_seconds must be removed (#77 — replaced by Phase.ANSWER)."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(DevLoopInput)}
    assert "question_timeout_seconds" not in field_names


def test_from_env_does_not_read_question_timeout_seconds(monkeypatch):
    """QUESTION_TIMEOUT_SECONDS is no longer read by from_env (#77)."""
    monkeypatch.setenv("QUESTION_TIMEOUT_SECONDS", "900")
    inp = DevLoopInput.from_env("omneval")
    assert not hasattr(inp, "question_timeout_seconds")


# --------------------------------------------------------------------------- #
# Phase.ANSWER + max_questions_per_phase (#77)
# --------------------------------------------------------------------------- #
def test_phase_enum_has_answer():
    """Phase.ANSWER replaces the old chat-based human-reply loop."""
    from devloop.shared import Phase

    assert Phase.ANSWER.value == "answer"


def test_devloop_input_has_max_questions_per_phase():
    """max_questions_per_phase defaults to 3."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(DevLoopInput)}
    assert "max_questions_per_phase" in field_names
    assert DevLoopInput("omneval").max_questions_per_phase == 3


def test_from_env_reads_max_questions_per_phase(monkeypatch):
    monkeypatch.setenv("MAX_QUESTIONS_PER_PHASE", "5")
    inp = DevLoopInput.from_env("omneval")
    assert inp.max_questions_per_phase == 5


def test_from_env_max_questions_per_phase_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("MAX_QUESTIONS_PER_PHASE", raising=False)
    monkeypatch.setenv("MAX_QUESTIONS_PER_PHASE", "not-a-number")
    inp = DevLoopInput.from_env("omneval")
    assert inp.max_questions_per_phase == DevLoopInput.max_questions_per_phase


# --------------------------------------------------------------------------- #
# Phase enum — no MERGE (#74)
# --------------------------------------------------------------------------- #
def test_phase_enum_no_merge():
    """Phase.MERGE must be removed from the Phase enum."""
    from devloop.shared import Phase

    assert not hasattr(Phase, "MERGE")
    # Other phases still present
    assert hasattr(Phase, "PLAN")
    assert hasattr(Phase, "EXECUTE")
    assert hasattr(Phase, "REVIEW")


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
    result = await _env_and_run(DevLoopInput("omneval"), [])
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
    result = await _env_and_run(DevLoopInput("omneval"), [])
    assert result.status == "completed"
    assert "post_pr_comments" not in M.dispatched_phases


# --------------------------------------------------------------------------- #
# Reviewer notification after review (#74)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_reviewer_notification_comment_after_review(reset_mocks):
    """After the review phase, a GitHub comment is posted with the PR URL and
    @mentions the pr_reviewer."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    result = await _env_and_run(DevLoopInput("omneval"), [])
    assert result.status == "completed"
    # The notification comment mentions "ready for review"
    assert any("ready for review" in n.lower() for n in M.notifications)
    # The reviewer activity was called
    assert len(M.reviewer_requests) >= 1


@pytest.mark.asyncio
async def test_notify_reviewer_does_not_claim_tagged_when_no_reviewer_configured(
    reset_mocks,
):
    """issue #88: when request_github_reviewer reports it skipped the request
    (e.g. no pr_reviewer configured for the project), the 'ready for review'
    comment must not claim a reviewer was tagged — that would mislead the
    human who's supposed to act on it."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.reviewer_result = ReviewerRequestResult(
        requested=False, reason="no reviewer is configured for this project"
    )
    result = await _env_and_run(DevLoopInput("omneval"), [])
    assert result.status == "completed"

    ready_comments = [n for n in M.notifications if "ready for review" in n.lower()]
    assert ready_comments, "expected a 'ready for review' notification comment"
    comment = ready_comments[0]
    assert "tagged" not in comment.lower()
    assert "no reviewer was requested" in comment.lower()
    assert "no reviewer is configured for this project" in comment.lower()


# --------------------------------------------------------------------------- #
# Phase.CI_FIX loop (#76)
# --------------------------------------------------------------------------- #
def test_phase_enum_has_ci_fix_no_remediation():
    """Phase.CI_FIX replaces the removed Phase.REMEDIATION."""
    from devloop.shared import Phase

    assert Phase.CI_FIX.value == "ci_fix"
    assert not hasattr(Phase, "REMEDIATION")


def test_devloop_input_has_execute_max_iterations():
    """execute_max_iterations defaults to 1."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(DevLoopInput)}
    assert "execute_max_iterations" in field_names
    assert DevLoopInput("omneval").execute_max_iterations == 1


def test_from_env_reads_execute_max_iterations(monkeypatch):
    monkeypatch.setenv("EXECUTE_MAX_ITERATIONS", "4")
    inp = DevLoopInput.from_env("omneval")
    assert inp.execute_max_iterations == 4


def test_from_env_execute_max_iterations_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("EXECUTE_MAX_ITERATIONS", raising=False)
    monkeypatch.setenv("EXECUTE_MAX_ITERATIONS", "not-a-number")
    inp = DevLoopInput.from_env("omneval")
    assert inp.execute_max_iterations == DevLoopInput.execute_max_iterations


def test_devloop_input_has_ci_fix_max_iterations():
    """ci_fix_max_iterations defaults to 5."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(DevLoopInput)}
    assert "ci_fix_max_iterations" in field_names
    assert DevLoopInput("omneval").ci_fix_max_iterations == 5


def test_from_env_reads_ci_fix_max_iterations(monkeypatch):
    monkeypatch.setenv("CI_FIX_MAX_ITERATIONS", "3")
    inp = DevLoopInput.from_env("omneval")
    assert inp.ci_fix_max_iterations == 3


def test_from_env_ci_fix_max_iterations_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("CI_FIX_MAX_ITERATIONS", raising=False)
    monkeypatch.setenv("CI_FIX_MAX_ITERATIONS", "not-a-number")
    inp = DevLoopInput.from_env("omneval")
    assert inp.ci_fix_max_iterations == DevLoopInput.ci_fix_max_iterations


@pytest.mark.asyncio
async def test_ci_fix_loop_exits_early_when_ci_passes_on_second_iteration(reset_mocks):
    """CI is red on iteration 1 (a fix is dispatched), green on iteration 2 —
    the loop exits early after a single fix attempt, reporting exhausted=False."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.ci_poll_results = [
        CIChecksResult(
            all_passed=False,
            failures=[CICheckFailure(name="pytest", conclusion="failure")],
        ),
        CIChecksResult(all_passed=True, failures=[]),
    ]
    result = await _env_and_run(
        DevLoopInput("omneval", ci_fix_max_iterations=5),
        [],
    )
    assert result.status == "completed"
    # CI went green on the second poll — only one fix attempt was dispatched
    assert M.dispatched_phases.count("ci_fix") == 1
    assert len(M.ci_polls) == 2
    # queued comment precedes the dispatch
    queued = [
        n for n in M.notifications if "queued" in n.lower() and "ci fix" in n.lower()
    ]
    assert len(queued) == 1
    assert "1/5" in queued[0]
    # result comment reports attempt N/max with the commit count
    attempts = [
        n
        for n in M.notifications
        if ("🔧" in n or "❌ ci fix attempt" in n.lower())
        and "ci fix attempt" in n.lower()
    ]
    assert len(attempts) == 1
    assert "1/5" in attempts[0]
    assert "pushed 1 commit" in attempts[0].lower()
    # CI passed before exhausting — no "still failing" note to the reviewer
    assert not any("still failing" in n.lower() for n in M.notifications)


@pytest.mark.asyncio
async def test_ci_fix_loop_exhausts_after_max_iterations(reset_mocks):
    """CI never goes green — the loop runs ci_fix_max_iterations times, returns
    exhausted=True, and the reviewer notification carries a CI-still-failing note."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.ci_poll_results = [
        CIChecksResult(
            all_passed=False,
            failures=[CICheckFailure(name="pytest", conclusion="failure")],
        ),
    ]
    result = await _env_and_run(
        DevLoopInput("omneval", ci_fix_max_iterations=2),
        [],
    )
    assert result.status == "completed"
    assert M.dispatched_phases.count("ci_fix") == 2
    attempts = [
        n
        for n in M.notifications
        if ("🔧" in n or "❌ ci fix attempt" in n.lower())
        and "ci fix attempt" in n.lower()
    ]
    assert len(attempts) == 2
    assert any("2/2" in a for a in attempts)
    # the workflow continued on to review + reviewer notification with the
    # "still failing" note carried from the exhausted ci_fix loop
    assert any("still failing" in n.lower() for n in M.notifications)


@pytest.mark.asyncio
async def test_ci_fix_loop_not_exhausted_when_final_attempt_fixes_ci(reset_mocks):
    """CI is still red after the second-to-last poll but the final fix attempt
    turns it green — the loop must re-check before reporting exhausted=True,
    otherwise the reviewer gets a false "still failing" note on a green PR."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.ci_poll_results = [
        CIChecksResult(
            all_passed=False,
            failures=[CICheckFailure(name="pytest", conclusion="failure")],
        ),
        CIChecksResult(
            all_passed=False,
            failures=[CICheckFailure(name="pytest", conclusion="failure")],
        ),
        CIChecksResult(all_passed=True, failures=[]),
    ]
    result = await _env_and_run(
        DevLoopInput("omneval", ci_fix_max_iterations=2),
        [],
    )
    assert result.status == "completed"
    # both allotted attempts were dispatched, then a final re-poll found CI green
    assert M.dispatched_phases.count("ci_fix") == 2
    assert len(M.ci_polls) == 3
    assert not any("still failing" in n.lower() for n in M.notifications)


@pytest.mark.asyncio
async def test_ci_fix_loop_dispatches_with_failure_details(reset_mocks):
    """Each ci_fix dispatch carries the current failing check details in
    TaskSpec.extra['ci_check_failures'] and is preceded by a queued comment."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.ci_poll_results = [
        CIChecksResult(
            all_passed=False,
            failures=[
                CICheckFailure(name="pytest", conclusion="failure", summary="3 failed")
            ],
        ),
        CIChecksResult(all_passed=True, failures=[]),
    ]

    result = await _env_and_run(DevLoopInput("omneval", ci_fix_max_iterations=5), [])

    assert result.status == "completed"
    ci_fix_specs = [s for s in M.dispatched_specs if s.get("phase") == "ci_fix"]
    assert ci_fix_specs, "expected at least one ci_fix dispatch"
    extra = ci_fix_specs[0]["extra"]
    assert "ci_check_failures" in extra
    failures = extra["ci_check_failures"]
    assert failures and failures[0]["name"] == "pytest"
    assert failures[0]["summary"] == "3 failed"


@pytest.mark.asyncio
async def test_ci_fix_loop_waits_on_pending_checks_without_dispatching_fix(reset_mocks):
    """issue #90: checks that are merely still running (no genuine failures
    yet) must make the loop wait and re-poll — not dispatch a Phase.CI_FIX
    job or post a fix-attempt comment, and not consume one of the limited
    ci_fix_max_iterations attempts."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.ci_poll_results = [
        CIChecksResult(all_passed=False, pending=True, failures=[]),
        CIChecksResult(all_passed=False, pending=True, failures=[]),
        CIChecksResult(all_passed=True, pending=False, failures=[]),
    ]
    result = await _env_and_run(
        DevLoopInput("omneval", ci_fix_max_iterations=2),
        [],
    )
    assert result.status == "completed"
    # no fix attempt was dispatched — CI was merely slow, never genuinely red
    assert M.dispatched_phases.count("ci_fix") == 0
    assert len(M.ci_polls) == 3
    assert not any("ci fix attempt" in n.lower() for n in M.notifications)
    assert not any("queued — ci fix" in n.lower() for n in M.notifications)
    assert not any("still failing" in n.lower() for n in M.notifications)


@pytest.mark.asyncio
async def test_ci_fix_loop_comments_post_to_pr_number_not_issue_number(reset_mocks):
    """CI-fix-loop status comments ("queued — CI fix attempt...", "pushed N
    commit(s)", "failed") narrate work happening on the PR — they must land on
    the PR thread (PR #501), not the original issue (#1), even though the two
    are distinct numbers."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.dispatch_behavior[("execute", 1)] = AgentJobResult(
        status=JobStatus.COMPLETE.value,
        job_name="j1",
        issue_number=1,
        branch="agent/issue-1",
        pr_url="https://github.com/omneval/omneval/pull/501",
        commits=1,
        tests_passed=True,
    )
    reset_mocks.ci_poll_results = [
        CIChecksResult(
            all_passed=False,
            failures=[CICheckFailure(name="pytest", conclusion="failure")],
        ),
        CIChecksResult(all_passed=True, failures=[]),
    ]
    result = await _env_and_run(
        DevLoopInput("omneval", ci_fix_max_iterations=5),
        [],
    )
    assert result.status == "completed"

    ci_fix_comments = [
        c for c in M.github_comments if "ci fix attempt" in c.body.lower()
    ]
    assert ci_fix_comments, "expected CI-fix-loop comments to be posted"
    assert all(c.issue_number == 501 for c in ci_fix_comments), (
        "CI-fix-loop comments must target the PR (#501), not the issue (#1) — "
        f"got issue_numbers {[c.issue_number for c in ci_fix_comments]}"
    )


@pytest.mark.asyncio
async def test_multiple_rounds_accumulate_queued_for_review(reset_mocks):
    """Each completed issue is added to queued_for_review across rounds."""
    reset_mocks.plan_rounds = [_one_issue(1), _one_issue(2)]
    result = await _env_and_run(DevLoopInput("omneval", max_iterations=5), [])
    assert result.status == "completed"
    assert 1 in result.queued_for_review
    assert 2 in result.queued_for_review


# --------------------------------------------------------------------------- #
# Remediation phase (#56)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_remediation_dispatched_between_execute_and_review(reset_mocks):
    """Remediation is inserted between Execute and Review in the workflow."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.poll_pr_checks_result = {"failures": ["lint failed"]}
    result = await _env_and_run(DevLoopInput("omneval"), ["approve", "approve"])
    assert result.status == "completed"
    assert result.queued_for_review == [1]
    phases = M.dispatched_phases
    assert "execute" in phases
    assert "remediation" in phases
    assert "review" in phases
    assert phases.index("remediation") > phases.index("execute")
    assert phases.index("remediation") < phases.index("review")


@pytest.mark.asyncio
async def test_remediation_no_op_when_checks_pass(reset_mocks):
    """When all CI checks pass, remediation is a no-op (no agent dispatched)."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.poll_pr_checks_result = {"failures": []}  # all checks pass
    result = await _env_and_run(DevLoopInput("omneval"), ["approve", "approve"])
    assert result.status == "completed"
    phases = M.dispatched_phases
    assert "remediation" not in phases


@pytest.mark.asyncio
async def test_remediation_parks_issue_on_failure(reset_mocks):
    """When remediation produces zero commits, the issue is parked with a
    notification comment and the review phase is skipped for that round."""
    reset_mocks.plan_rounds = [_one_issue(1)]
    reset_mocks.poll_pr_checks_result = {"failures": ["check-a failed"]}
    reset_mocks.remediation_commits = 0  # remediation produced no fix
    result = await _env_and_run(DevLoopInput("omneval"), ["approve", "approve"])
    assert result.status == "completed"
    phases = M.dispatched_phases
    assert "remediation" in phases
    # Review and merge must NOT be dispatched after a parked issue
    assert "review" not in phases
    assert "merge" not in phases
    # A notification comment was posted
    notifications = M.notifications
    assert any("Parked" in msg and "remediation failed" in msg for msg in notifications)
