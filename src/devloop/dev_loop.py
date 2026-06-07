"""Dev Loop Temporal workflow (issues #20-#23) — sequential model.

Mirrors the Sandcastle loop: each round the planner picks the next unblocked
issue, a human approves it (Plan gate), the implementer works it, the reviewer
refines it, and after a Merge gate the merger merges + closes it. The loop
repeats so newly-unblocked issues are picked up after each merge.

    ┌────────────────────────── round ──────────────────────────┐
    Plan ─▶ [Plan gate] ─▶ Execute ─▶ Review ─▶ [Merge gate] ─▶ Merge
    └──────────────────────── repeat ───────────────────────────┘

One issue at a time: the homelab DGX model serves a single request at a time,
so parallel agent Jobs would just block on inference. Each phase is a K8s
Agent Job driven by a bundled prompt (plan/implement/review/merge).
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
    DispatchInput,
    GithubNotificationInput,
    InlineComment,
    JobStatus,
    OpenAgentPRsInput,
    PostCommentsInput,
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
    # Plan/merge human-approval gates. Without a bound, a forgotten
    # approval parks the run forever, and because the webhook reuses the
    # devloop-<project> workflow id (USE_EXISTING), every later issue is then
    # silently dropped. On timeout the  plan gate pauses the loop and the
    # merge gate leaves the PR open and moves on.
    gate_timeout_seconds: float = 14400.0  # 4h plan/merge approval gate
    replan_max: int = 3
    poll_interval_seconds: float = 5.0

    @classmethod
    def from_env(
        cls, project_id: str, agent_label: str = "agent-ready"
    ) -> "DevLoopInput":
        """Build an input with the timeout gates sourced from the worker env.

        Called only from the webhook/schedule entry points, which run in the
        worker process (outside the Temporal workflow sandbox), so reading
        os.environ here is safe — the resolved values then travel inside the
        serialized input and the workflow itself never touches the environment.

        ``GATE_TIMEOUT_SECONDS`` / ``QUESTION_TIMEOUT_SECONDS`` are wired by the
        Helm chart (templates/temporal-worker-deployment.yaml). Each falls back
        to the dataclass default above, so the Helm value and the Python default
        stay in sync. A missing or malformed value is tolerated and falls back.
        """
        import os

        def _seconds(name: str, default: float) -> float:
            try:
                return float(os.environ[name])
            except (KeyError, ValueError):
                return default

        return cls(
            project_id=project_id,
            agent_label=agent_label,
            question_timeout_seconds=_seconds(
                "QUESTION_TIMEOUT_SECONDS", cls.question_timeout_seconds
            ),
            gate_timeout_seconds=_seconds(
                "GATE_TIMEOUT_SECONDS", cls.gate_timeout_seconds
            ),
        )


@dataclass
class DevLoopResult:
    status: str  # completed | paused | failed_plan | failed_merge
    merged_issues: list[int] = field(default_factory=list)
    detail: str = ""


# Sentinel returned by the plan phase when the plan gate times out (distinct from
# None, which means the plan was actively rejected past replan_max).
_PLAN_GATE_TIMEOUT = object()


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
        """Block for the next unconsumed human reply. None on timeout."""
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
        thread_name = f"{inp.project_id} — Dev Loop"
        merged: list[int] = []

        for rnd in range(1, inp.max_iterations + 1):
            plan = await self._plan_phase(inp, thread_name, rnd)
            if plan is _PLAN_GATE_TIMEOUT:
                return DevLoopResult(
                    "paused", merged_issues=merged, detail="plan gate timed out"
                )
            if plan is None:
                return DevLoopResult(
                    "failed_plan", merged_issues=merged, detail="plan rejected"
                )
            issues = plan.get("issues") or []
            if not issues:
                workflow.logger.info(
                    "No unblocked agent-ready issues remain — Dev Loop complete for %s",
                    inp.project_id,
                )
                return DevLoopResult("completed", merged_issues=merged)

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

            outcome = await self._merge_phase(inp, issue, exec_result, thread_name)
            if outcome == "merged":
                merged.append(_as_int(issue.get("id")))
            elif outcome == "failed":
                return DevLoopResult(
                    "failed_merge", merged_issues=merged, detail=f"#{issue.get('id')}"
                )

        workflow.logger.info(
            "Reached max iterations (%d) — pausing Dev Loop for %s.",
            inp.max_iterations,
            inp.project_id,
        )
        return DevLoopResult("completed", merged_issues=merged)

    # ---- Plan phase + gate (#20) --------------------------------------- #
    async def _plan_phase(self, inp: DevLoopInput, thread_name: str, rnd: int):
        replans = 0
        feedback = ""
        while True:
            spec = TaskSpec(
                phase="plan",
                project_id=inp.project_id,
                extra={"agent_label": inp.agent_label, "feedback": feedback},
            )
            result = await self._dispatch(inp, spec)
            plan = result.plan or {"issues": []}
            issues = plan.get("issues") or []
            issues = await self._drop_issues_in_review(inp, issues)
            plan = {**plan, "issues": issues}
            if not issues:
                return plan  # run() turns an empty plan into a completed result

            # Post the plan to the next issue's GitHub thread
            next_issue_no = _as_int(issues[0].get("id"))
            await self._comment(
                inp.project_id,
                next_issue_no,
                logic.render_plan(inp.project_id, rnd, issues),
            )
            reply = await self._await_reply(timeout=inp.gate_timeout_seconds)
            if reply is None:
                # No approval within the gate window. Pause rather than auto-run
                # an unreviewed plan; a Closed run lets the next labeled issue
                # start fresh instead of parking this one forever.
                await self._comment(
                    inp.project_id,
                    next_issue_no,
                    "⏸️ Plan gate timed out with no approval — pausing Dev Loop. "
                    "Re-label an issue to resume.",
                )
                return _PLAN_GATE_TIMEOUT
            if logic.is_approval(reply):
                return plan
            replans += 1
            if replans > inp.replan_max:
                await self._comment(
                    inp.project_id,
                    next_issue_no,
                    f"❌ Plan rejected {inp.replan_max} times — aborting Dev Loop.",
                )
                return None
            feedback = reply or ""

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
            return {"issue_id": issue_no, "branch": "", "pr_url": "", "commits": 0}
        if result.commits:
            await self._comment(
                inp.project_id,
                issue_no,
                f"✅ Implemented — PR: {result.pr_url or result.branch}",
            )
        return {
            "issue_id": issue_no,
            "branch": result.branch,
            "pr_url": result.pr_url,
            "commits": result.commits,
        }

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

    # ---- Merge gate + Merge (#23) -------------------------------------- #
    async def _merge_phase(
        self, inp: DevLoopInput, issue: dict, exec_result: dict, thread_name: str
    ) -> str:
        issue_no = _as_int(issue.get("id"))
        await self._comment(
            inp.project_id,
            issue_no,
            logic.merge_gate_message(issue, exec_result["pr_url"]),
        )
        reply = await self._await_reply(timeout=inp.gate_timeout_seconds)
        if reply is None:
            # No merge decision within the gate window. Leave the PR open for a
            # human to merge later and move on; _drop_issues_in_review keeps this
            # open-PR issue out of future plan rounds so it won't re-prompt.
            await self._comment(
                inp.project_id,
                issue_no,
                f"⏱️ #{issue_no} merge gate timed out — leaving the PR open "
                "and moving on to other issues.",
            )
            return "skipped"
        if not logic.is_approval(reply):
            await self._comment(
                inp.project_id,
                issue_no,
                f"#{issue_no} not approved for merge — skipping.",
            )
            return "skipped"

        spec = TaskSpec(
            phase="merge",
            project_id=inp.project_id,
            issue_number=issue_no,
            extra={
                "branches": [exec_result["branch"]],
                "issues": [
                    {"id": str(issue.get("id")), "title": issue.get("title", "")}
                ],
            },
        )
        await self._comment(
            inp.project_id,
            issue_no,
            "⏳ queued — agent is merging this issue",
        )
        merge = await self._dispatch(inp, spec, issue_number=issue_no)
        if merge.status != JobStatus.COMPLETE.value:
            await self._comment(
                inp.project_id,
                issue_no,
                f"❌ Parked — CI fix exhausted after 5 attempts\n"
                f"{merge.error or merge.summary}",
            )
            return "failed"

        await self._comment(
            inp.project_id,
            issue_no,
            f"📬 Opened review PR for #{issue_no}: "
            f"{merge.pr_url or '(branch pushed)'} "
            "— tagged the reviewer. Approve & merge it on GitHub to close the issue.",
        )
        return "merged"
