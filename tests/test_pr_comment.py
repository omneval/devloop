"""PRCommentWorkflow tests (issue #78) using Temporal's time-skipping env with
mocked activities on the orchestration / job-dispatch task queues.

Mirrors the harness in test_dev_loop.py: a module-level ``Mocks`` dataclass
configures activity behavior per test, and ``_env_and_run`` spins up a
WorkflowEnvironment with both task queues registered.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict as dataclasses_asdict, dataclass, field

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from devloop.pr_comment import PRCommentInput, PRCommentWorkflow
from devloop.shared import (
    JOB_DISPATCH_QUEUE,
    ORCHESTRATION_QUEUE,
    AgentJobResult,
    CICheckFailure,
    CIChecksResult,
    GithubNotificationInput,
    JobStatus,
)


@dataclass
class Mocks:
    pr_comment_status: str = JobStatus.COMPLETE.value
    pr_comment_commits: int = 1
    pr_comment_pr_url: str = "https://github.com/omneval/omneval/pull/17"
    pr_comment_summary: str = "Pushed `abc1234`: renamed the helper per feedback."
    ci_poll_results: list = field(default_factory=list)
    ci_poll_calls: int = 0
    ci_fix_commits: list = field(default_factory=lambda: [1])
    ci_fix_status: str = JobStatus.COMPLETE.value
    pr_branch_result: str = ""

    github_comments: list = field(default_factory=list)
    dispatched_phases: list = field(default_factory=list)
    dispatched_specs: list = field(default_factory=list)
    reviewer_requests: list = field(default_factory=list)
    ci_polls: list = field(default_factory=list)
    pr_branch_lookups: list = field(default_factory=list)

    @property
    def notifications(self):
        return [c.body for c in self.github_comments]


M = Mocks()


def _make_activities():
    @activity.defn(name="dispatch_agent_job")
    async def dispatch_agent_job(inp) -> AgentJobResult:
        spec = inp["task_spec"] if isinstance(inp, dict) else inp.task_spec
        phase = spec["phase"] if isinstance(spec, dict) else spec.phase
        issue = inp["issue_number"] if isinstance(inp, dict) else inp.issue_number
        M.dispatched_phases.append(phase)
        M.dispatched_specs.append(
            spec if isinstance(spec, dict) else dataclasses_asdict(spec)
        )
        if phase == "pr_comment":
            return AgentJobResult(
                status=M.pr_comment_status,
                job_name=f"pc{issue}",
                issue_number=issue,
                branch=f"agent/issue-{issue}",
                pr_url=M.pr_comment_pr_url,
                commits=M.pr_comment_commits,
                summary=M.pr_comment_summary,
                error="" if M.pr_comment_status == JobStatus.COMPLETE.value else "boom",
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

    @activity.defn(name="post_github_comment")
    async def post_github_comment(inp: GithubNotificationInput) -> None:
        M.github_comments.append(inp)

    @activity.defn(name="request_github_reviewer")
    async def request_github_reviewer(inp) -> None:
        M.reviewer_requests.append(inp)

    @activity.defn(name="poll_ci_checks")
    async def poll_ci_checks(inp) -> CIChecksResult:
        M.ci_polls.append(inp)
        results = M.ci_poll_results
        if not results:
            return CIChecksResult(all_passed=True, failures=[])
        idx = min(M.ci_poll_calls, len(results) - 1)
        M.ci_poll_calls += 1
        return results[idx]

    @activity.defn(name="get_pr_branch")
    async def get_pr_branch(inp) -> str:
        M.pr_branch_lookups.append(inp)
        return M.pr_branch_result

    @activity.defn(name="cleanup_configmap")
    async def cleanup_configmap(job_name: str) -> None:
        pass

    return {
        "dispatch": [dispatch_agent_job],
        "orchestration": [
            post_github_comment,
            request_github_reviewer,
            poll_ci_checks,
            get_pr_branch,
            cleanup_configmap,
        ],
    }


@pytest.fixture
def reset_mocks():
    global M
    M = Mocks()
    return M


async def _run_pr_comment(client: Client, inp: PRCommentInput):
    wf_id = f"pr-comment-test-{uuid.uuid4().hex[:8]}"
    handle = await client.start_workflow(
        PRCommentWorkflow.run, inp, id=wf_id, task_queue=ORCHESTRATION_QUEUE
    )
    return await handle.result()


async def _env_and_run(inp: PRCommentInput):
    acts = _make_activities()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with (
            Worker(
                env.client,
                task_queue=ORCHESTRATION_QUEUE,
                workflows=[PRCommentWorkflow],
                activities=acts["orchestration"],
            ),
            Worker(
                env.client,
                task_queue=JOB_DISPATCH_QUEUE,
                workflows=[],
                activities=acts["dispatch"],
            ),
        ):
            return await _run_pr_comment(env.client, inp)


def _input(**overrides):
    base = dict(
        project_id="omneval",
        pr_number=17,
        issue_number=53,
        branch="agent/issue-53",
        comment_body="Please rename this function.",
        source="review",
        author="a-human-reviewer",
    )
    base.update(overrides)
    return PRCommentInput(**base)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_pr_comment_workflow_full_flow(reset_mocks):
    """Posts "queued", dispatches Phase.PR_COMMENT with PR diff + comment body
    in TaskSpec.extra, runs the CI fix loop (passes immediately), requests a
    reviewer, and posts a result comment."""
    result = await _env_and_run(_input())

    assert result.status == "completed"
    assert result.commits == 1
    assert result.exhausted is False

    # "⏳ queued" comment posted first
    queued = [n for n in M.notifications if "queued" in n.lower()]
    assert queued, "expected a queued comment"
    assert "responding to reviewer feedback" in queued[0].lower()

    # Phase.PR_COMMENT dispatched with PR number + comment body in extra
    # (the agent fetches the diff itself via `gh pr diff` — see _fetch_pr_diff —
    # rather than the workflow threading it through TASK_SPEC, which broke for
    # large PRs: a big diff blew past Linux's per-env-var size limit)
    assert "pr_comment" in M.dispatched_phases
    pr_comment_specs = [s for s in M.dispatched_specs if s.get("phase") == "pr_comment"]
    assert pr_comment_specs
    extra = pr_comment_specs[0]["extra"]
    assert "pr_diff" not in extra
    assert extra["comment_body"] == "Please rename this function."
    assert extra["pr_number"] == 17
    assert extra["source"] == "review"

    # CI checks were polled (loop ran, passed immediately => no ci_fix dispatch)
    assert len(M.ci_polls) == 1
    assert "ci_fix" not in M.dispatched_phases

    # reviewer requested
    assert len(M.reviewer_requests) == 1

    # result comment posted, including the agent's summary referencing the SHA
    assert any("addressed your feedback" in n.lower() for n in M.notifications)
    assert any("abc1234" in n for n in M.notifications)


@pytest.mark.asyncio
async def test_pr_comment_workflow_runs_ci_fix_loop_on_red_ci(reset_mocks):
    """When CI is red after the pr_comment dispatch, the CI fix cycle kicks in
    before the reviewer is notified."""
    reset_mocks.ci_poll_results = [
        CIChecksResult(
            all_passed=False,
            failures=[CICheckFailure(name="pytest", conclusion="failure")],
        ),
        CIChecksResult(all_passed=True, failures=[]),
    ]

    result = await _env_and_run(_input())

    assert result.status == "completed"
    assert result.exhausted is False
    assert M.dispatched_phases.count("ci_fix") == 1
    assert len(M.ci_polls) == 2
    assert any("ci fix attempt" in n.lower() for n in M.notifications)
    # reviewer still requested after the loop completes
    assert len(M.reviewer_requests) == 1


@pytest.mark.asyncio
async def test_pr_comment_workflow_exhausts_ci_fix_and_notes_reviewer(reset_mocks):
    """CI never goes green — the loop exhausts, exhausted=True is reported,
    and the result comment carries a "still failing" note."""
    reset_mocks.ci_poll_results = [
        CIChecksResult(
            all_passed=False,
            failures=[CICheckFailure(name="pytest", conclusion="failure")],
        ),
    ]

    result = await _env_and_run(_input(ci_fix_max_iterations=2))

    assert result.status == "completed"
    assert result.exhausted is True
    assert M.dispatched_phases.count("ci_fix") == 2
    assert any("still failing" in n.lower() for n in M.notifications)


@pytest.mark.asyncio
async def test_pr_comment_workflow_handles_phase_failure(reset_mocks):
    """When the Phase.PR_COMMENT dispatch fails outright, the workflow posts a
    failure comment and returns status=failed without running the CI fix loop
    or requesting a reviewer."""
    reset_mocks.pr_comment_status = JobStatus.FAILED.value

    result = await _env_and_run(_input())

    assert result.status == "failed"
    assert any("could not respond" in n.lower() for n in M.notifications)
    assert "ci_fix" not in M.dispatched_phases
    assert M.reviewer_requests == []


@pytest.mark.asyncio
async def test_pr_comment_workflow_carries_issue_comment_context(reset_mocks):
    """An issue_comment-sourced run carries the comment body + source/author
    through to the dispatched TaskSpec.extra."""
    result = await _env_and_run(
        _input(
            source="comment",
            author="another-human",
            comment_body="@devloop-bot please tweak the docstring",
        )
    )

    assert result.status == "completed"
    pr_comment_specs = [s for s in M.dispatched_specs if s.get("phase") == "pr_comment"]
    extra = pr_comment_specs[0]["extra"]
    assert extra["source"] == "comment"
    assert extra["author"] == "another-human"
    assert extra["comment_body"] == "@devloop-bot please tweak the docstring"


# --------------------------------------------------------------------------- #
# Branch resolution (issue #101)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_pr_comment_workflow_resolves_branch_when_missing(reset_mocks):
    """``issue_comment`` (an ``@devloop-bot`` mention) webhook payloads carry no
    ``pull_request.head.ref``, so ``PRCommentInput.branch`` arrives empty. The
    workflow must resolve the real branch via ``get_pr_branch`` before
    dispatching — an empty branch makes the agent's ``git clone --branch ''``
    fail outright (BackoffLimitExceeded)."""
    reset_mocks.pr_branch_result = "agent/issue-53-rename-helper"

    result = await _env_and_run(_input(branch="", source="comment"))

    assert result.status == "completed"
    assert len(M.pr_branch_lookups) == 1
    looked_up = M.pr_branch_lookups[0]
    assert looked_up["project_id"] == "omneval"
    assert looked_up["pr_number"] == 17

    pr_comment_specs = [s for s in M.dispatched_specs if s.get("phase") == "pr_comment"]
    assert pr_comment_specs[0]["branch"] == "agent/issue-53-rename-helper"


@pytest.mark.asyncio
async def test_pr_comment_workflow_skips_lookup_when_branch_known(reset_mocks):
    """A ``pull_request_review`` payload already carries the head branch — the
    workflow must dispatch with it directly, without calling ``get_pr_branch``."""
    result = await _env_and_run(_input(branch="agent/issue-53", source="review"))

    assert result.status == "completed"
    assert M.pr_branch_lookups == []
    pr_comment_specs = [s for s in M.dispatched_specs if s.get("phase") == "pr_comment"]
    assert pr_comment_specs[0]["branch"] == "agent/issue-53"


@pytest.mark.asyncio
async def test_pr_comment_workflow_fails_cleanly_when_branch_unresolvable(reset_mocks):
    """When the branch can't be resolved (e.g. the PR vanished), the workflow
    must fail cleanly with an explanatory comment rather than dispatch an
    Agent Execution Job doomed to ``git clone --branch ''``."""
    reset_mocks.pr_branch_result = ""

    result = await _env_and_run(_input(branch="", source="comment"))

    assert result.status == "failed"
    assert "branch resolution failed" in result.detail
    assert any("could not resolve" in n.lower() for n in M.notifications)
    assert "pr_comment" not in M.dispatched_phases
    assert M.reviewer_requests == []


@pytest.mark.asyncio
async def test_pr_comment_workflow_refuses_non_agent_branch(reset_mocks):
    """A resolved branch that doesn't match ``agent/issue-<N>`` means the
    mentioned PR isn't agent-owned — dispatching would push (with ``force``)
    to a human's branch. The workflow must refuse and fail cleanly instead."""
    reset_mocks.pr_branch_result = "a-humans-feature-branch"

    result = await _env_and_run(_input(branch="", source="comment"))

    assert result.status == "failed"
    assert "not an agent-owned branch" in result.detail
    assert any("isn't an agent-owned pr" in n.lower() for n in M.notifications)
    assert "pr_comment" not in M.dispatched_phases
    assert M.reviewer_requests == []
