"""Dev Loop Temporal workflow (issues #20-#23, #74) — fully autonomous model.

Once an issue is labelled ``agent-ready`` the workflow runs autonomously
through to reviewer notification with no human-approval gates.

    ┌──────────────────────────── round ─────────────────────────────┐
    Plan ─▶ Execute ─▶ Review ─▶ Request Reviewer + Notify
    └───────────────────────────── repeat ───────────────────────────┘

One issue at a time: the homelab DGX model serves a single request at a time,
so parallel agent Jobs would just block on inference. Each phase is a K8s
Agent Job driven by a bundled prompt (plan/implement/review).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from . import dev_loop_logic as logic
from .shared import (
    AgentJobResult,
    AnswerInput,
    AwaitInput,
    CIChecksResult,
    DispatchInput,
    GithubNotificationInput,
    InlineComment,
    JOB_DISPATCH_QUEUE,
    JobStatus,
    OpenAgentPRsInput,
    Phase,
    PollCIChecksInput,
    PostCommentsInput,
    RequestReviewerInput,
    TaskSpec,
)


# --------------------------------------------------------------------------- #
# Workflow input / result
# --------------------------------------------------------------------------- #
@dataclass
class DevLoopInput:
    project_id: str
    agent_label: str = "agent-ready"
    max_iterations: int = 30
    # configurable down to seconds for tests
    question_timeout_seconds: float = 14400.0  # 4h mid-run gate
    poll_interval_seconds: float = 5.0
    # Phase.CI_FIX loop: retry until CI is green or this many attempts are spent.
    ci_fix_max_iterations: int = 5

    @classmethod
    def from_env(
        cls, project_id: str, agent_label: str = "agent-ready"
    ) -> "DevLoopInput":
        """Build an input with the timeout gates sourced from the worker env.

        Called only from the webhook/schedule entry points, which run in the
        worker process (outside the Temporal workflow sandbox), so reading
        os.environ here is safe — the resolved values then travel inside the
        serialized input and the workflow itself never touches the environment.

        ``QUESTION_TIMEOUT_SECONDS`` is wired by the Helm chart
        (templates/temporal-worker-deployment.yaml). Falls back to the dataclass
        default above, so the Helm value and the Python default stay in sync.
        A missing or malformed value is tolerated and falls back.
        """
        import os

        def _seconds(name: str, default: float) -> float:
            try:
                return float(os.environ[name])
            except (KeyError, ValueError):
                return default

        def _int(name: str, default: int) -> int:
            try:
                return int(os.environ[name])
            except (KeyError, ValueError):
                return default

        return cls(
            project_id=project_id,
            agent_label=agent_label,
            question_timeout_seconds=_seconds(
                "QUESTION_TIMEOUT_SECONDS", cls.question_timeout_seconds
            ),
            ci_fix_max_iterations=_int(
                "CI_FIX_MAX_ITERATIONS", cls.ci_fix_max_iterations
            ),
        )


@dataclass
class DevLoopResult:
    status: str  # completed | failed_plan
    queued_for_review: list[int] = field(default_factory=list)
    detail: str = ""


_RETRY = RetryPolicy(maximum_attempts=3)
_ACTIVITY_TIMEOUT = timedelta(hours=2)
_GITHUB_COMMENT_TIMEOUT = timedelta(seconds=60)


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@workflow.defn
class DevLoopWorkflow:
    def __init__(self) -> None:
        self._replies: list[str] = []
        self._consumed = 0
        self._ask_lock: asyncio.Lock | None = None

    # ---- signals -------------------------------------------------------- #
    @workflow.signal
    def human_reply(self, text: str) -> None:
        self._replies.append(text)

    # ---- GitHub Issue comment helper ------------------------------------ #
    async def _comment(self, inp_project_id: str, issue_number: int, body: str) -> None:
        """Post a comment on the given GitHub Issue via devloop-bot."""
        await workflow.execute_activity(
            "post_github_comment",
            GithubNotificationInput(
                issue_number=issue_number,
                project_id=inp_project_id,
                body=body,
            ),
            start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _await_reply(self, timeout: float | None = None) -> str | None:
        """Block for the next unconsumed human reply. None on timeout.

        Used only by ``_answer_questions`` for mid-run clarifying questions
        (``Phase.ANSWER``); the plan and merge gates have been removed.
        """
        target = self._consumed + 1
        try:
            await workflow.wait_condition(
                lambda: len(self._replies) >= target,
                timeout=timedelta(seconds=timeout) if timeout else None,
            )
        except asyncio.TimeoutError:
            return None
        reply = self._replies[self._consumed]
        self._consumed += 1
        return reply

    async def _dispatch(
        self, inp: DevLoopInput, spec: TaskSpec, issue_number: int = 0
    ) -> AgentJobResult:
        return await workflow.execute_activity(
            "dispatch_agent_job",
            DispatchInput(
                inp.project_id,
                issue_number,
                spec,
                poll_interval_seconds=inp.poll_interval_seconds,
            ),
            result_type=AgentJobResult,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
            task_queue=JOB_DISPATCH_QUEUE,
        )

    async def _drop_issues_in_review(
        self, inp: DevLoopInput, issues: list[dict]
    ) -> list[dict]:
        """Drop planned issues that already have an open agent PR.

        Under the PR-review merge model an issue stays open until a human merges
        its PR, so the planner would otherwise re-surface it every round. We ask
        GitHub which issues already have an ``agent/issue-<N>`` PR open and filter
        them out, telling the channel they're parked on review."""
        if not issues:
            return issues
        in_review = await workflow.execute_activity(
            "open_agent_pr_issue_numbers",
            OpenAgentPRsInput(inp.project_id),
            result_type=list,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_RETRY,
        )
        in_review = {_as_int(n) for n in (in_review or [])}
        if not in_review:
            return issues
        kept, skipped = [], []
        for issue in issues:
            (skipped if _as_int(issue.get("id")) in in_review else kept).append(issue)
        if skipped:
            for sk in skipped:
                sk_no = _as_int(sk.get("id"))
                await self._comment(
                    inp.project_id,
                    sk_no,
                    f"⏭️ Skipping #{sk_no} — already has an open review PR awaiting merge.",
                )
        return kept

    # ---- run ------------------------------------------------------------ #
    @workflow.run
    async def run(self, inp: DevLoopInput) -> DevLoopResult:
        self._ask_lock = asyncio.Lock()
        queued: list[int] = []

        for rnd in range(1, inp.max_iterations + 1):
            plan = await self._plan_phase(inp, rnd)
            if plan is None:
                return DevLoopResult(
                    "failed_plan", queued_for_review=queued, detail="plan rejected"
                )
            issues = plan.get("issues") or []
            if not issues:
                workflow.logger.info(
                    "No unblocked agent-ready issues remain — Dev Loop complete for %s",
                    inp.project_id,
                )
                return DevLoopResult("completed", queued_for_review=queued)

            issue = issues[0]  # sequential: work one issue per round
            exec_result = await self._execute_phase(inp, issue)
            if not exec_result["commits"]:
                await self._comment(
                    inp.project_id,
                    _as_int(issue.get("id")),
                    f"⚠️ #{issue.get('id')} produced no commits — skipping this round.",
                )
                continue

            await self._review_phase(inp, issue, exec_result)
            await self._notify_reviewer(inp, issue, exec_result)
            queued.append(_as_int(issue.get("id")))

        workflow.logger.info(
            "Reached max iterations (%d) — pausing Dev Loop for %s.",
            inp.max_iterations,
            inp.project_id,
        )
        return DevLoopResult("completed", queued_for_review=queued)

    # ---- Plan phase (#20, #74) ----------------------------------------- #
    async def _plan_phase(self, inp: DevLoopInput, rnd: int) -> dict | None:
        """Dispatch the Plan Agent Execution Job and return the plan directly.

        No human-approval gate. ``_drop_issues_in_review`` filters out any
        issues that already have an open agent PR so the planner doesn't
        re-surface them each round.
        """
        spec = TaskSpec(
            phase="plan",
            project_id=inp.project_id,
            extra={"agent_label": inp.agent_label},
        )
        result = await self._dispatch(inp, spec)
        plan = result.plan or {"issues": []}
        issues = plan.get("issues") or []
        issues = await self._drop_issues_in_review(inp, issues)
        return {**plan, "issues": issues}

    # ---- Execute phase (#21) ------------------------------------------- #
    async def _execute_phase(self, inp: DevLoopInput, issue: dict) -> dict:
        issue_no = _as_int(issue.get("id"))
        spec = TaskSpec(
            phase="execute",
            project_id=inp.project_id,
            issue_number=issue_no,
            title=issue.get("title", ""),
            branch=issue.get("branch", ""),
        )
        await self._comment(
            inp.project_id,
            issue_no,
            "⏳ queued — agent is working on this issue",
        )
        result = await self._dispatch(inp, spec, issue_number=issue_no)
        result = await self._answer_questions(inp, issue_no, result)

        if result.status != JobStatus.COMPLETE.value:
            await self._comment(
                inp.project_id,
                issue_no,
                f"❌ Parked — execute phase failed: {result.error or 'unknown error'}",
            )
            return {
                "issue_id": issue_no,
                "branch": "",
                "pr_url": "",
                "commits": 0,
                "exhausted": False,
            }
        if result.commits:
            await self._comment(
                inp.project_id,
                issue_no,
                f"✅ Implemented — PR: {result.pr_url or result.branch}",
            )
        exec_result = {
            "issue_id": issue_no,
            "branch": result.branch,
            "pr_url": result.pr_url,
            "commits": result.commits,
            "exhausted": False,
        }
        exhausted = await self._ci_fix_loop(inp, issue_no, exec_result)
        exec_result["exhausted"] = exhausted
        return exec_result

    # ---- Phase.CI_FIX loop (#76) ---------------------------------------- #
    async def _ci_fix_loop(
        self, inp: DevLoopInput, issue_no: int, exec_result: dict
    ) -> bool:
        """Retry CI fixes up to ``ci_fix_max_iterations`` times or until green.

        Each iteration polls the PR's CI checks; if they all pass, the loop
        exits early (``exhausted=False``). Otherwise it dispatches a
        ``Phase.CI_FIX`` Agent Execution Job — preceded by a "⏳ queued"
        comment — with the current failing check details in
        ``TaskSpec.extra["ci_check_failures"]``, then posts a result comment.

        Returns ``True`` when every iteration is spent without CI going green
        (``exhausted``), so ``_notify_reviewer`` can carry a "CI still failing"
        note to the human reviewer.
        """
        pr_number = logic.pr_number_from_url(exec_result.get("pr_url", ""))
        if pr_number <= 0:
            return False

        max_iters = inp.ci_fix_max_iterations
        for attempt in range(1, max_iters + 1):
            checks = await workflow.execute_activity(
                "poll_ci_checks",
                PollCIChecksInput(project_id=inp.project_id, pr_number=pr_number),
                result_type=CIChecksResult,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=_RETRY,
            )
            if checks.all_passed:
                return False

            failures = [
                {
                    "name": f.name,
                    "conclusion": f.conclusion,
                    "details_url": f.details_url,
                    "summary": f.summary,
                }
                for f in (checks.failures or [])
            ]
            spec = TaskSpec(
                phase=Phase.CI_FIX.value,
                project_id=inp.project_id,
                issue_number=issue_no,
                branch=exec_result.get("branch", ""),
                extra={"ci_check_failures": failures},
            )
            await self._comment(
                inp.project_id,
                issue_no,
                f"⏳ queued — CI fix attempt {attempt}/{max_iters}",
            )
            result = await self._dispatch(inp, spec, issue_number=issue_no)
            if result.status == JobStatus.COMPLETE.value:
                await self._comment(
                    inp.project_id,
                    issue_no,
                    f"🔧 CI fix attempt {attempt}/{max_iters} — "
                    f"pushed {result.commits} commit(s)",
                )
            else:
                await self._comment(
                    inp.project_id,
                    issue_no,
                    f"❌ CI fix attempt {attempt}/{max_iters} failed",
                )

        return True

    async def _answer_questions(
        self, inp: DevLoopInput, issue_no: int, result: AgentJobResult
    ) -> AgentJobResult:
        while result.status == JobStatus.AWAITING_HUMAN.value:
            async with self._ask_lock:
                await self._comment(
                    inp.project_id,
                    issue_no,
                    f"❓ [#{issue_no}] {result.question}",
                )
                answer = await self._await_reply(timeout=inp.question_timeout_seconds)
            if answer is None:
                answer = (
                    "No human reply within the timeout — proceed with your best guess."
                )
                await self._comment(
                    inp.project_id,
                    issue_no,
                    f"⏱️ [#{issue_no}] no reply — proceeding with best-guess.",
                )
            await workflow.execute_activity(
                "answer_agent_job",
                AnswerInput(result.job_name, answer),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=_RETRY,
            )
            result = await workflow.execute_activity(
                "await_agent_job",
                AwaitInput(
                    result.job_name,
                    poll_interval_seconds=inp.poll_interval_seconds,
                ),
                result_type=AgentJobResult,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_RETRY,
            )
        return result

    # ---- Review phase (#22) -------------------------------------------- #
    async def _review_phase(
        self, inp: DevLoopInput, issue: dict, exec_result: dict
    ) -> None:
        issue_no = _as_int(issue.get("id"))
        spec = TaskSpec(
            phase="review",
            project_id=inp.project_id,
            issue_number=issue_no,
            branch=exec_result["branch"],
        )
        await self._comment(
            inp.project_id,
            issue_no,
            "⏳ queued — agent is reviewing this issue",
        )
        result = await self._dispatch(inp, spec, issue_number=issue_no)
        if result.commits:
            await self._comment(
                inp.project_id,
                issue_no,
                f"🔎 Reviewed #{issue_no} — pushed {result.commits} refinement commit(s).",
            )
        else:
            await self._comment(
                inp.project_id,
                issue_no,
                f"🔎 Reviewed #{issue_no} — no changes needed.",
            )
        await self._post_review_findings(inp, exec_result, result)

    async def _post_review_findings(
        self, inp: DevLoopInput, exec_result: dict, result: AgentJobResult
    ) -> None:
        """Post the reviewer's findings to the PR.

        Raises ``RuntimeError`` when findings exist but the PR URL cannot be
        resolved (unparseable or missing), so the failure surfaces rather than
        silently dropping review comments.
        """
        review = result.review or {}
        summary = review.get("summary", "")
        inline = [
            InlineComment(
                file=c.get("file", ""),
                line=_as_int(c.get("line")),
                body=c.get("body", ""),
            )
            for c in (review.get("inline_comments") or [])
        ]
        if not summary and not inline:
            return
        pr_url = exec_result.get("pr_url", "")
        pr_number = logic.pr_number_from_url(pr_url)
        if not pr_number:
            raise RuntimeError(
                f"cannot post review findings: pr_url '{pr_url}' "
                f"for project {inp.project_id} is unparseable or missing"
            )
        await workflow.execute_activity(
            "post_pr_comments",
            PostCommentsInput(inp.project_id, pr_number, summary, inline),
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_RETRY,
        )

    # ---- Reviewer notification (#74) ------------------------------------ #
    async def _notify_reviewer(
        self, inp: DevLoopInput, issue: dict, exec_result: dict
    ) -> None:
        """Request a GitHub PR reviewer and post a notification comment.

        Reads ``pr_reviewer`` from the project's ``ProjectConfig``. When no
        reviewer is configured the comment still fires but omits the @-mention.
        """
        issue_no = _as_int(issue.get("id"))
        pr_url = exec_result.get("pr_url", "")
        pr_number = logic.pr_number_from_url(pr_url)

        # Resolve the configured reviewer from the project registry.
        # We import get_project inside workflow.now() context; the registry is
        # process-wide so this is safe (no I/O). In tests the value is empty.
        # We use workflow.execute_activity for the actual reviewer request so
        # the I/O stays in activities, not in the workflow sandbox.
        await workflow.execute_activity(
            "request_github_reviewer",
            RequestReviewerInput(
                project_id=inp.project_id,
                pr_number=pr_number,
                reviewer="",  # reviewer resolved by the activity from project registry
            ),
            start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
            retry_policy=_RETRY,
        )

        note = (
            " ⚠️ CI is still failing after exhausting the CI fix attempts —"
            " please take a look."
            if exec_result.get("exhausted")
            else ""
        )
        await self._comment(
            inp.project_id,
            issue_no,
            f"👀 Ready for review — PR: {pr_url}. Reviewer has been tagged.{note}",
        )
