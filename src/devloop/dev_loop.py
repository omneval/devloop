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

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from . import dev_loop_logic as logic
from ._workflow_common import _WorkflowCommon
from .shared import (
    AgentJobResult,
    AnswerInput,
    AwaitInput,
    InlineComment,
    JobStatus,
    OpenAgentPRsInput,
    Phase,
    PollPRChecksInput,
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
    poll_interval_seconds: float = 5.0
    # Phase.CI_FIX loop: retry until CI is green or this many attempts are spent.
    ci_fix_max_iterations: int = 5
    # Execute phase: retry the dispatch this many times when it produces zero
    # commits before parking the issue with a "skipping this round" comment.
    execute_max_iterations: int = 1
    # Mid-run AWAITING_HUMAN questions (#77): how many Phase.ANSWER jobs a single
    # phase run may spawn before the workflow stops asking and tells the parked
    # job to proceed with its best guess.
    max_questions_per_phase: int = 3
    # The issue whose `agent_label` triggered this run. Scopes the Plan phase
    # to that single issue instead of replanning the whole agent-ready backlog
    # — see _plan_phase. 0 only for legacy/test inputs; the webhook always
    # supplies a real issue number since it's the sole entry point.
    triggering_issue: int = 0

    @classmethod
    def from_env(
        cls,
        project_id: str,
        agent_label: str = "agent-ready",
        triggering_issue: int = 0,
    ) -> "DevLoopInput":
        """Build an input with the timeout gates sourced from the worker env.

        Called only from the webhook/schedule entry points, which run in the
        worker process (outside the Temporal workflow sandbox), so reading
        os.environ here is safe — the resolved values then travel inside the
        serialized input and the workflow itself never touches the environment.

        Falls back to the dataclass defaults above, so the Helm values and the
        Python defaults stay in sync. A missing or malformed value is tolerated
        and falls back.
        """
        import os

        def _int(name: str, default: int) -> int:
            try:
                return int(os.environ[name])
            except (KeyError, ValueError):
                return default

        return cls(
            project_id=project_id,
            agent_label=agent_label,
            triggering_issue=triggering_issue,
            ci_fix_max_iterations=_int(
                "CI_FIX_MAX_ITERATIONS", cls.ci_fix_max_iterations
            ),
            execute_max_iterations=_int(
                "EXECUTE_MAX_ITERATIONS", cls.execute_max_iterations
            ),
            max_questions_per_phase=_int(
                "MAX_QUESTIONS_PER_PHASE", cls.max_questions_per_phase
            ),
        )


@dataclass
class DevLoopResult:
    status: str  # completed | failed_plan
    queued_for_review: list[int] = field(default_factory=list)
    detail: str = ""
    review_verdicts: dict[int, str] = field(default_factory=dict)


_RETRY = RetryPolicy(maximum_attempts=3)
_ACTIVITY_TIMEOUT = timedelta(hours=2)
_GITHUB_COMMENT_TIMEOUT = timedelta(seconds=60)


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@workflow.defn
class DevLoopWorkflow(_WorkflowCommon):
    def __init__(self) -> None:
        pass

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
        queued: list[int] = []
        verdicts: dict[int, str] = {}

        for rnd in range(1, inp.max_iterations + 1):
            plan = await self._plan_phase(inp, rnd)
            if plan is None:
                return DevLoopResult(
                    "failed_plan",
                    queued_for_review=queued,
                    detail="plan rejected",
                    review_verdicts=verdicts,
                )
            issues = plan.get("issues") or []
            if not issues:
                workflow.logger.info(
                    "No unblocked agent-ready issues remain — Dev Loop complete for %s",
                    inp.project_id,
                )
                return DevLoopResult(
                    "completed",
                    queued_for_review=queued,
                    review_verdicts=verdicts,
                )

            issue = issues[0]  # sequential: work one issue per round
            exec_result = await self._execute_phase(inp, issue)
            if not exec_result["commits"]:
                # _execute_phase already posted the failure/exhaustion comment
                # and parked the issue — move on to the next issue this round.
                continue

            parked = await self._remediation_phase(inp, issue, exec_result)
            if parked:
                continue

            verdict = await self._review_phase(inp, issue, exec_result)
            await self._notify_reviewer(inp, issue, exec_result)
            queued.append(_as_int(issue.get("id")))
            if verdict:
                verdicts[_as_int(issue.get("id"))] = verdict

        workflow.logger.info(
            "Reached max iterations (%d) — pausing Dev Loop for %s.",
            inp.max_iterations,
            inp.project_id,
        )
        return DevLoopResult(
            "completed", queued_for_review=queued, review_verdicts=verdicts
        )

    # ---- Plan phase (#20, #74) ----------------------------------------- #
    async def _plan_phase(self, inp: DevLoopInput, rnd: int) -> dict | None:
        """Dispatch the Plan Agent Execution Job and return the plan directly.

        Scoped to ``inp.triggering_issue`` — the issue whose ``agent_label``
        triggered this run — rather than the entire agent-ready backlog. A
        labeling event signals intent to work *that* issue; replanning across
        every open issue let the agent pick a different (and possibly much
        larger) one to execute first, surprising whoever applied the label.

        No human-approval gate. ``_drop_issues_in_review`` filters out the
        issue if it already has an open agent PR (e.g. a prior round already
        queued it) so the planner doesn't re-surface it.
        """
        spec = TaskSpec(
            phase="plan",
            project_id=inp.project_id,
            issue_number=inp.triggering_issue,
            extra={"agent_label": inp.agent_label},
        )
        result = await self._dispatch(
            inp.project_id, spec, poll_interval_seconds=inp.poll_interval_seconds
        )
        plan = result.plan or {"issues": []}
        issues = plan.get("issues") or []
        issues = await self._drop_issues_in_review(inp, issues)
        return {**plan, "issues": issues}

    # ---- Execute phase (#21, #75) --------------------------------------- #
    async def _execute_phase(self, inp: DevLoopInput, issue: dict) -> dict:
        """Dispatch the Execute Agent Execution Job, retrying on zero commits.

        If the dispatch produces zero commits, the workflow retries up to
        ``execute_max_iterations`` times — each attempt preceded by a "⏳
        queued" comment. Once an attempt produces commits (or fails outright),
        the retry loop stops and the result is processed normally (including
        the CI fix loop). If every attempt produces zero commits, the issue is
        parked with a "❌ Execute exhausted ..." comment and the round moves on
        to the next issue (no CI fix loop, no reviewer notification).
        """
        issue_no = _as_int(issue.get("id"))
        spec = TaskSpec(
            phase="execute",
            project_id=inp.project_id,
            issue_number=issue_no,
            title=issue.get("title", ""),
            branch=issue.get("branch", ""),
        )

        max_iters = inp.execute_max_iterations
        result = None
        for attempt in range(1, max_iters + 1):
            await self._comment(
                inp.project_id,
                issue_no,
                "⏳ queued — agent is working on this issue",
            )
            result = await self._dispatch(
                inp.project_id,
                spec,
                issue_number=issue_no,
                poll_interval_seconds=inp.poll_interval_seconds,
            )
            result = await self._answer_questions(inp, issue_no, result)

            if result.status != JobStatus.COMPLETE.value or result.commits:
                break
            # Zero commits and status == COMPLETE — retry (loop continues).

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

        if not result.commits:
            await self._comment(
                inp.project_id,
                issue_no,
                f"❌ Execute exhausted {max_iters} attempts with no commits"
                " — skipping this round",
            )
            return {
                "issue_id": issue_no,
                "branch": "",
                "pr_url": "",
                "commits": 0,
                "exhausted": False,
            }

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
        exhausted = await self._ci_fix_loop(
            inp.project_id,
            issue_no,
            exec_result,
            ci_fix_max_iterations=inp.ci_fix_max_iterations,
            poll_interval_seconds=inp.poll_interval_seconds,
        )
        exec_result["exhausted"] = exhausted
        return exec_result

    async def _answer_via_agent(
        self, inp: DevLoopInput, issue_no: int, question: str, branch: str
    ) -> str:
        """Spawn a fresh ``Phase.ANSWER`` Agent Execution Job to answer a
        mid-run clarifying question (#77).

        The job gets the question and working-branch access via ``TaskSpec`` so
        it can investigate the codebase before answering; its
        ``AgentJobResult.summary`` is returned as the best-informed answer.
        """
        await self._comment(
            inp.project_id,
            issue_no,
            "⏳ queued — answering agent question",
        )
        spec = TaskSpec(
            phase=Phase.ANSWER.value,
            project_id=inp.project_id,
            issue_number=issue_no,
            branch=branch,
            extra={"question": question},
        )
        answer_result = await self._dispatch(
            inp.project_id,
            spec,
            issue_number=issue_no,
            poll_interval_seconds=inp.poll_interval_seconds,
        )
        answer = answer_result.summary or "proceed with your best guess"
        await self._comment(
            inp.project_id,
            issue_no,
            f"🤔 Agent asked: {question} → Auto-answered by agent: {answer}",
        )
        return answer

    async def _answer_questions(
        self, inp: DevLoopInput, issue_no: int, result: AgentJobResult
    ) -> AgentJobResult:
        """Resolve mid-run ``AWAITING_HUMAN`` pauses without a human.

        Each question dispatches a fresh ``Phase.ANSWER`` Agent Execution Job
        (counted against ``maxConcurrentJobs`` via ``JOB_DISPATCH_QUEUE``) that
        investigates the question with access to the working branch; its
        answer is patched back into the paused job's ConfigMap via
        ``answer_agent_job`` and the original job resumes via
        ``await_agent_job``.

        Once the phase run has spawned ``max_questions_per_phase`` answer jobs,
        the workflow stops asking and tells the parked job to proceed with its
        best guess directly (no further answer jobs spawned).
        """
        questions_asked = 0
        while result.status == JobStatus.AWAITING_HUMAN.value:
            question = result.question
            if questions_asked >= inp.max_questions_per_phase:
                answer = "proceed with your best guess"
                await self._comment(
                    inp.project_id,
                    issue_no,
                    "⚠️ Question limit reached — agent proceeding with best "
                    f"guess. Question was: {question}",
                )
            else:
                questions_asked += 1
                answer = await self._answer_via_agent(
                    inp, issue_no, question, result.branch
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

    # ---- Remediation phase (#56) --------------------------------------- #
    async def _remediation_phase(
        self, inp: DevLoopInput, issue: dict, exec_result: dict
    ) -> bool:
        """Run the Remediation phase between Execute and Review.

        Polls CI checks on the draft PR. If all checks pass (or none exist)
        this is a no-op. If checks are failing, one Agent Execution Job is
        dispatched with the remediation prompt. On failure the issue is
        parked with a notification comment.

        Returns True if the issue was parked (caller should ``continue`` to
        the next round), False otherwise.
        """
        issue_no = _as_int(issue.get("id"))
        pr_url = exec_result.get("pr_url", "")
        pr_number = logic.pr_number_from_url(pr_url)

        # Poll CI check runs on the PR
        checks = await workflow.execute_activity(
            "poll_pr_checks",
            PollPRChecksInput(
                inp.project_id,
                pr_number,
                timeout_seconds=inp.poll_interval_seconds,
            ),
            result_type=dict,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_RETRY,
        )

        # No-op when all checks pass or no checks exist
        failures = checks.get("failures", [])
        if not failures:
            return False

        # Dispatch remediation agent job with failing checks context
        spec = TaskSpec(
            phase="remediation",
            project_id=inp.project_id,
            issue_number=issue_no,
            branch=exec_result.get("branch", ""),
            extra={
                "ci_check_failures": "\n".join(failures),
            },
        )
        result = await self._dispatch(
            inp.project_id,
            spec,
            issue_number=issue_no,
            poll_interval_seconds=inp.poll_interval_seconds,
        )

        # If remediation produced no commits or failed, park the issue
        if result.status != JobStatus.COMPLETE.value or not result.commits:
            await self._park_issue(inp.project_id, issue_no, failures)
            return True  # skip to next round

        await self._comment(
            inp.project_id,
            issue_no,
            f"🔧 Remediated #{issue_no} — pushed {result.commits} fix commit(s).",
        )
        return False

    async def _park_issue(
        self, project_id: str, issue_no: int, failures: list[str]
    ) -> None:
        """Post a notification comment and park the issue for this round."""
        summary = "\n".join(failures[:3])  # cap to 3 failures in notification
        await self._comment(
            project_id,
            issue_no,
            f"🅿️  Parked #{issue_no} — remediation failed. Failing checks:\n{summary}",
        )

    # ---- Review phase (#22, #55) --------------------------------------- #
    async def _review_phase(
        self, inp: DevLoopInput, issue: dict, exec_result: dict
    ) -> str | None:
        """Review the PR and return a verdict string (lgtm/changes_requested/blocked)."""
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
        result = await self._dispatch(
            inp.project_id,
            spec,
            issue_number=issue_no,
            poll_interval_seconds=inp.poll_interval_seconds,
        )
        verdict: str | None = None
        review = result.review or {}
        if review:
            verdict = review.get("verdict")
            if verdict:
                await self._comment(
                    inp.project_id,
                    issue_no,
                    f"🔎 Reviewed #{issue_no} — verdict: {verdict}.",
                )
            else:
                await self._comment(
                    inp.project_id,
                    issue_no,
                    "🔎 Reviewed #{issue_no} — no changes needed.",
                )
        else:
            await self._comment(
                inp.project_id,
                issue_no,
                "🔎 Reviewed #{issue_no} — no changes needed.",
            )
        await self._post_review_findings(inp, exec_result, result)
        return verdict

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

        Reads ``pr_reviewer`` from the project's ``ProjectConfig`` (via the
        ``request_github_reviewer`` activity). The notification only claims a
        reviewer was tagged when the request actually succeeded — when it was
        skipped (no reviewer configured, no PR to request on) or failed (a
        GitHub API error), the comment says so honestly instead (issue #88);
        a confidently-wrong "tagged" claim would mislead the human who's
        supposed to act on it.
        """
        issue_no = _as_int(issue.get("id"))
        pr_url = exec_result.get("pr_url", "")
        pr_number = logic.pr_number_from_url(pr_url)

        # Resolve the configured reviewer from the project registry.
        # We use the shared _request_reviewer helper (mixin) so the I/O stays
        # in activities, not in the workflow sandbox — the activity resolves
        # the actual reviewer login from the project registry and reports
        # back whether the request actually succeeded.
        reviewer_result = await self._request_reviewer(inp.project_id, pr_number)
        if reviewer_result.requested:
            reviewer_note = "Reviewer has been tagged."
        elif reviewer_result.reason:
            reviewer_note = f"No reviewer was requested ({reviewer_result.reason})."
        else:
            reviewer_note = "No reviewer was requested."

        note = (
            " ⚠️ CI is still failing after exhausting the CI fix attempts —"
            " please take a look."
            if exec_result.get("exhausted")
            else ""
        )
        await self._comment(
            inp.project_id,
            issue_no,
            f"👀 Ready for review — PR: {pr_url}. {reviewer_note}{note}",
        )
