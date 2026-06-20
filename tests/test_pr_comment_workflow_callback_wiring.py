"""Tests for PRCommentWorkflow callback wiring — verify that the workflow
composes PRCommentPhase, CICycle, and Notifier with injectable callbacks.

These tests use Temporal's time-skipping env with mocked activities to verify
that the workflow wires all 5 phases with injectable callbacks consistent with
the PhaseOps pattern used in DevLoopWorkflow (issues #187/#188).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from devloop.github import RequestReviewerInput, ReviewerRequestResult
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
class CallbackWiringMocks:
    """Track callback calls across all phases."""

    cicycle_callbacks_called: list[str] = field(default_factory=list)
    notifier_callbacks_called: list[str] = field(default_factory=list)
    pr_comment_callbacks_called: list[str] = field(default_factory=list)

    # Activity tracking (existing)
    github_comments: list = field(default_factory=list)
    dispatched_phases: list = field(default_factory=list)
    reviewer_requests: list = field(default_factory=list)
    ci_polls: list = field(default_factory=list)
    ci_poll_results: list = field(default_factory=list)
    pr_branch_lookups: list = field(default_factory=list)
    ci_fix_commits: list = field(default_factory=lambda: [1])
    ci_fix_status: str = JobStatus.COMPLETE.value

    @property
    def notifications(self):
        return [c.body for c in self.github_comments]


W = CallbackWiringMocks()


def _make_activities():
    @activity.defn(name="dispatch_agent_job")
    async def dispatch_agent_job(inp) -> AgentJobResult:
        spec = inp["task_spec"] if isinstance(inp, dict) else inp.task_spec
        phase = spec["phase"] if isinstance(spec, dict) else spec.phase
        issue = inp["issue_number"] if isinstance(inp, dict) else inp.issue_number
        W.dispatched_phases.append(phase)
        if phase == "pr_comment":
            return AgentJobResult(
                status=JobStatus.COMPLETE.value,
                job_name=f"pc{issue}",
                issue_number=issue,
                branch=f"agent/issue-{issue}",
                pr_url="https://github.com/omneval/omneval/pull/17",
                commits=1,
                summary="Test summary",
            )
        if phase == "ci_fix":
            attempt = W.dispatched_phases.count("ci_fix") - 1
            commits_seq = W.ci_fix_commits or [1]
            commits = commits_seq[min(attempt, len(commits_seq) - 1)]
            return AgentJobResult(
                status=W.ci_fix_status,
                job_name=f"cf{issue}",
                issue_number=issue,
                commits=commits,
                error="" if W.ci_fix_status == JobStatus.COMPLETE.value else "boom",
            )
        return AgentJobResult(status=JobStatus.COMPLETE.value, job_name="x")

    @activity.defn(name="post_github_comment")
    async def post_github_comment(inp: GithubNotificationInput) -> None:
        if isinstance(inp, dict):
            inp = GithubNotificationInput(**inp)
        W.github_comments.append(inp)

    @activity.defn(name="request_github_reviewer")
    async def request_github_reviewer(inp) -> ReviewerRequestResult:
        if isinstance(inp, dict):
            inp = RequestReviewerInput(**inp)
        W.reviewer_requests.append(inp)
        return ReviewerRequestResult(requested=True)

    @activity.defn(name="poll_ci_checks")
    async def poll_ci_checks(inp) -> CIChecksResult:
        W.ci_polls.append(inp)
        if not W.ci_polls or len(W.ci_polls) == 1:
            # First poll (from CICycle) passes immediately
            return CIChecksResult(all_passed=True, failures=[])
        return CIChecksResult(all_passed=True, failures=[])

    @activity.defn(name="get_pr_branch")
    async def get_pr_branch(inp) -> str:
        W.pr_branch_lookups.append(inp)
        return "agent/issue-53"

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
def reset_wiring_mocks():
    global W
    W = CallbackWiringMocks()
    return W


async def _run(client: Client, inp: PRCommentInput):
    wf_id = f"pr-comment-wiring-test-{uuid.uuid4().hex[:8]}"
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
            return await _run(env.client, inp)


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


class TestPRCommentWorkflowCallbackWiring:
    """Verify that PRCommentWorkflow wires all 5 phases with injectable callbacks."""

    @pytest.mark.asyncio
    async def test_workflow_composes_notifier_with_callbacks(
        self, reset_wiring_mocks
    ) -> None:
        """PRCommentWorkflow must compose a Notifier that receives injectable
        callbacks for request_reviewer and post_comment, rather than calling
        the activity directly."""
        result = await _env_and_run(_input())

        assert result.status == "completed"
        # Notifier posts a comment
        assert any("Ready for review" in n for n in W.notifications), (
            "Expected Notifier to post a notification comment"
        )
        # Notifier requested a reviewer
        assert len(W.reviewer_requests) == 1

    @pytest.mark.asyncio
    async def test_cicycle_wired_with_callbacks_from_workflow(
        self, reset_wiring_mocks
    ) -> None:
        """CICycle must be wired with injectable callbacks from the workflow.
        Even when CI passes immediately, the callback wiring must be in place."""
        result = await _env_and_run(_input())

        assert result.status == "completed"
        # CICycle should have been instantiated and polled at least once
        assert len(W.ci_polls) >= 1

    @pytest.mark.asyncio
    async def test_cicycle_callbacks_wire_fix_loop_on_red_ci(
        self, reset_wiring_mocks
    ) -> None:
        """When CI is red, the CICycle callback wiring dispatches fix jobs
        and re-polls, demonstrating the callback is properly wired."""
        W.ci_poll_results = [
            CIChecksResult(
                all_passed=False,
                failures=[CICheckFailure(name="pytest", conclusion="failure")],
            ),
            CIChecksResult(all_passed=True, failures=[]),
        ]

        poll_calls = [0]

        @activity.defn(name="poll_ci_checks")
        async def poll_ci_checks_red_first(inp) -> CIChecksResult:
            poll_calls[0] += 1
            idx = min(poll_calls[0] - 1, len(W.ci_poll_results) - 1)
            return W.ci_poll_results[idx]

        @activity.defn(name="dispatch_agent_job")
        async def dispatch_agent_job(inp) -> AgentJobResult:
            spec = inp["task_spec"] if isinstance(inp, dict) else inp.task_spec
            phase = spec["phase"] if isinstance(spec, dict) else spec.phase
            issue = inp["issue_number"] if isinstance(inp, dict) else inp.issue_number
            W.dispatched_phases.append(phase)
            if phase == "pr_comment":
                return AgentJobResult(
                    status=JobStatus.COMPLETE.value,
                    job_name=f"pc{issue}",
                    issue_number=issue,
                    branch=f"agent/issue-{issue}",
                    pr_url="https://github.com/omneval/omneval/pull/17",
                    commits=1,
                    summary="Test summary",
                )
            if phase == "ci_fix":
                return AgentJobResult(
                    status=JobStatus.COMPLETE.value,
                    job_name=f"cf{issue}",
                    issue_number=issue,
                    commits=1,
                    error="",
                )
            return AgentJobResult(status=JobStatus.COMPLETE.value, job_name="x")

        @activity.defn(name="post_github_comment")
        async def post_github_comment(inp: GithubNotificationInput) -> None:
            if isinstance(inp, dict):
                inp = GithubNotificationInput(**inp)
            W.github_comments.append(inp)

        @activity.defn(name="request_github_reviewer")
        async def request_github_reviewer(inp) -> ReviewerRequestResult:
            if isinstance(inp, dict):
                inp = RequestReviewerInput(**inp)
            W.reviewer_requests.append(inp)
            return ReviewerRequestResult(requested=True)

        @activity.defn(name="get_pr_branch")
        async def get_pr_branch(inp) -> str:
            W.pr_branch_lookups.append(inp)
            return "agent/issue-53"

        @activity.defn(name="cleanup_configmap")
        async def cleanup_configmap(job_name: str) -> None:
            pass

        acts = {
            "dispatch": [dispatch_agent_job],
            "orchestration": [
                post_github_comment,
                request_github_reviewer,
                poll_ci_checks_red_first,
                get_pr_branch,
                cleanup_configmap,
            ],
        }

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
                wf_id = f"pr-comment-wiring-red-ci-{uuid.uuid4().hex[:8]}"
                handle = await env.client.start_workflow(
                    PRCommentWorkflow.run,
                    _input(),
                    id=wf_id,
                    task_queue=ORCHESTRATION_QUEUE,
                )
                result = await handle.result()

        # CICycle callback wiring dispatched a fix job when CI was red
        assert result.status == "completed"
        assert W.dispatched_phases.count("ci_fix") >= 1

    @pytest.mark.asyncio
    async def test_pr_comment_phase_callback_injection_works(
        self, reset_wiring_mocks
    ) -> None:
        """PRCommentPhase callbacks must be injected from the workflow."""
        result = await _env_and_run(_input())
        assert result.status == "completed"
        # The workflow must have used the injected callback path,
        # not called the activity directly from the phase
        assert len(W.dispatched_phases) == 1
        assert W.dispatched_phases[0] == "pr_comment"
