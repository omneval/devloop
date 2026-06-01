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
    CHANNEL_APPROVALS,
    DISCORD_QUEUE,
    AgentJobResult,
    AnswerInput,
    AwaitInput,
    DispatchInput,
    JobStatus,
    OpenAgentPRsInput,
    SendMessageInput,
    SendNotificationInput,
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
    replan_max: int = 3
    poll_interval_seconds: float = 5.0


@dataclass
class DevLoopResult:
    status: str  # completed | failed_plan | failed_merge
    merged_issues: list[int] = field(default_factory=list)
    detail: str = ""


_RETRY = RetryPolicy(maximum_attempts=3)
_ACTIVITY_TIMEOUT = timedelta(hours=2)
_DISCORD_TIMEOUT = timedelta(seconds=60)


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

    # ---- discord helpers ------------------------------------------------ #
    def _wid(self) -> str:
        return workflow.info().workflow_id

    async def _say(
        self, message: str, thread_name: str = "", channel: str = CHANNEL_APPROVALS
    ) -> None:
        await workflow.execute_activity(
            "send_message",
            SendMessageInput(self._wid(), message, channel, thread_name),
            task_queue=DISCORD_QUEUE,
            start_to_close_timeout=_DISCORD_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _notify(self, message: str) -> None:
        await workflow.execute_activity(
            "send_notification",
            SendNotificationInput(self._wid(), message),
            task_queue=DISCORD_QUEUE,
            start_to_close_timeout=_DISCORD_TIMEOUT,
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
            await self._notify(
                "⏭️ Skipping "
                + ", ".join(f"#{i.get('id')}" for i in skipped)
                + " — already has an open review PR awaiting merge."
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
            if plan is None:
                return DevLoopResult(
                    "failed_plan", merged_issues=merged, detail="plan rejected"
                )
            issues = plan.get("issues") or []
            if not issues:
                await self._notify(
                    "No unblocked agent-ready issues remain — Dev Loop complete."
                )
                return DevLoopResult("completed", merged_issues=merged)

            issue = issues[0]  # sequential: work one issue per round
            exec_result = await self._execute_phase(inp, issue)
            if not exec_result["commits"]:
                await self._notify(
                    f"⚠️ #{issue.get('id')} produced no commits — skipping this round."
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

        await self._notify(
            f"Reached max iterations ({inp.max_iterations}) — pausing Dev Loop."
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

            await self._say(
                logic.render_plan(inp.project_id, rnd, issues), thread_name=thread_name
            )
            reply = await self._await_reply()  # plan gate does NOT time out
            if reply is not None and logic.is_approval(reply):
                return plan
            replans += 1
            if replans > inp.replan_max:
                await self._notify(
                    f"❌ Plan rejected {inp.replan_max} times — aborting Dev Loop."
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
        result = await self._dispatch(inp, spec, issue_number=issue_no)
        result = await self._answer_questions(inp, issue_no, spec, result)

        if result.status != JobStatus.COMPLETE.value:
            return {"issue_id": issue_no, "branch": "", "pr_url": "", "commits": 0}
        if result.commits:
            await self._notify(
                f"✅ Implemented #{issue_no} → {result.pr_url or result.branch}"
            )
        return {
            "issue_id": issue_no,
            "branch": result.branch,
            "pr_url": result.pr_url,
            "commits": result.commits,
        }

    async def _answer_questions(
        self, inp: DevLoopInput, issue_no: int, spec: TaskSpec, result: AgentJobResult
    ) -> AgentJobResult:
        while result.status == JobStatus.AWAITING_HUMAN.value:
            async with self._ask_lock:
                await self._say(f"❓ [#{issue_no}] {result.question}")
                answer = await self._await_reply(timeout=inp.question_timeout_seconds)
            if answer is None:
                answer = (
                    "No human reply within the timeout — proceed with your best guess."
                )
                await self._notify(
                    f"⏱️ [#{issue_no}] no reply — proceeding with best-guess."
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
                    inp.project_id,
                    issue_no,
                    spec,
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
        result = await self._dispatch(inp, spec, issue_number=issue_no)
        if result.commits:
            await self._notify(
                f"🔎 Reviewed #{issue_no} — pushed {result.commits} refinement commit(s)."
            )
        else:
            await self._notify(f"🔎 Reviewed #{issue_no} — no changes needed.")

    # ---- Merge gate + Merge (#23) -------------------------------------- #
    async def _merge_phase(
        self, inp: DevLoopInput, issue: dict, exec_result: dict, thread_name: str
    ) -> str:
        issue_no = _as_int(issue.get("id"))
        await self._say(
            logic.merge_gate_message(issue, exec_result["pr_url"]),
            thread_name=thread_name,
        )
        reply = await self._await_reply()  # merge gate does NOT time out
        if not (reply is not None and logic.is_approval(reply)):
            await self._notify(f"#{issue_no} not approved for merge — skipping.")
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
        merge = await self._dispatch(inp, spec, issue_number=issue_no)
        if merge.status != JobStatus.COMPLETE.value:
            await self._notify(
                f"❌ Merge #{issue_no} failed — manual intervention needed:\n"
                f"{merge.error or merge.summary}"
            )
            return "failed"

        await self._notify(
            f"📬 Opened review PR for #{issue_no}: {merge.pr_url or '(branch pushed)'} "
            "— tagged the reviewer. Approve & merge it on GitHub to close the issue."
        )
        return "merged"
