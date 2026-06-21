"""Dev Loop Temporal workflow (issues #20-#23, #74) — fully autonomous model.

Once an issue is labelled ``agent-ready`` the workflow runs autonomously
through to reviewer notification with no human-approval gates.

    ┌──────────────────────────── round ─────────────────────────────┐
    Plan ─▶ Execute ─▶ Review ─▶ Request Reviewer + Notify
    └───────────────────────────── repeat ───────────────────────────┘

One issue at a time: the homelab DGX model serves a single request at a time,
so parallel agent Jobs would just block on inference. Each phase is a K8s
Agent Job driven by a bundled prompt (plan/implement/review).

Architecture note (issue #149 / #153): the orchestration loop has been
extracted to ``devloop.phases.PhasePipeline`` and the per-phase logic
lives in standalone deep modules (``PlanPhase``, ``ExecutePhase``,
``ReviewPhase``, ``ReviewFixPass``, ``Notifier``).  This workflow is a
thin adapter — it inherits from ``PhaseOps``, delegates all its methods to
Temporal activity calls, and passes itself as the unified callback seam to
every phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional

from temporalio import workflow

from devloop.dev_loop_logic import pr_number_from_url
from ._constants import _ACTIVITY_TIMEOUT, _RETRY
from ._workflow_common import _WorkflowCommon
from .phases.execute import ExecutePhase
from .phases.notifier import Notifier
from .phases.phase_ops import PhaseOps
from .phases.pipeline import PhasePipeline
from .phases.plan import PlanPhase
from .phases.review import ReviewPhase
from .phases.review_fix_pass import ReviewFixPass
from .shared import (
    AgentJobResult,
    AnswerInput,
    AwaitInput,
    CIChecksResult,
    InlineComment,
    OpenAgentPRsInput,
    Phase,
    PlanIssueInput,
    PollCIChecksInput,
    PostCommentsInput,
    ReviewerRequestResult,
    TaskSpec,
    WorkflowKpiInput,
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
    # Review phase: when the reviewer's verdict is needs_fixes, dispatch a fix
    # pass addressing the findings and re-review, up to this many times, before
    # handing the PR to the human reviewer.
    review_fix_max_iterations: int = 1
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
            review_fix_max_iterations=_int(
                "REVIEW_FIX_MAX_ITERATIONS", cls.review_fix_max_iterations
            ),
        )


@dataclass
class DevLoopResult:
    status: str  # completed | failed_plan
    queued_for_review: list[int] = field(default_factory=list)
    detail: str = ""
    review_verdicts: dict[int, str] = field(default_factory=dict)


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@workflow.defn
class DevLoopWorkflow(_WorkflowCommon, PhaseOps):
    """Thin adapter: wires phase instances into PhasePipeline.

    All orchestration lives in ``PhasePipeline.run()`` — this class only
    creates phases, binds its own ``_WorkflowCommon`` methods as callbacks,
    and calls the pipeline.  Per-issue KPIs are emitted via a ``post_round``
    callback that the pipeline invokes after each successful round.

    Inherits from ``PhaseOps`` so the workflow itself is the unified callback
    seam: every phase receives ``self`` (a ``PhaseOps``) with all methods
    wired to Temporal activity calls.
    """

    def __init__(self) -> None:
        # Initialize PhaseOps base class — every field delegates to a Temporal
        # activity adapter method defined below.  ``drop_issues_in_review`` is
        # a no-op at init time and gets re-bound in ``_plan_phase_adapter``
        # with a closure over the current ``inp``.
        PhaseOps.__init__(
            self,
            comment=self._comment_activity,
            cleanup=self._cleanup_activity,
            dispatch=self._dispatch_activity,
            kpi_bump=self._kpi_bump_activity,
            kpi_take=self._kpi_take_activity,
            emit_kpis=self._emit_kpis_activity,
            poll_ci=self._poll_ci_activity,
            request_reviewer=self._request_reviewer_activity,
            dispatch_execute=self._dispatch_execute_activity,
            answer_question=self._answer_question_activity,
            dispatch_review=self._dispatch_review_activity,
            post_review_findings=self._post_review_findings_activity,
            dispatch_fix=self._dispatch_fix_activity,
            plan_issue=self._plan_issue_activity,
            dispatch_plan=self._dispatch_plan_activity,
            drop_issues_in_review=self._drop_issues_in_review,
        )
        # Lazy-init: KPI counters are workflow state and must survive replay.
        self._plan_phase_instance: PlanPhase | None = None
        self._execute_phase_instance: ExecutePhase | None = None
        self._review_phase_instance: ReviewPhase | None = None
        self._review_fix_pass_instance: ReviewFixPass | None = None
        self._notifier_instance: Notifier | None = None
        # Issues labelled while this run is already busy with another one
        # (issue #184) — queued here instead of being dropped, and drained
        # by the pipeline once the current triggering_issue has no more
        # rounds to plan.
        self._queued_issues: list[int] = []

    @workflow.signal
    def enqueue_issue(self, issue_number: int) -> None:
        """Queue an issue labelled ``agent_label`` while this workflow is
        already running another one (see ``webhook_deep.create_devloop_input``,
        issue #184)."""
        if issue_number and issue_number not in self._queued_issues:
            self._queued_issues.append(issue_number)

    def _dequeue_issue(self) -> int:
        return self._queued_issues.pop(0) if self._queued_issues else 0

    # ---- lazy phase constructors ---------------------------------------- #
    def _plan(self) -> PlanPhase:
        if self._plan_phase_instance is None:
            self._plan_phase_instance = PlanPhase()
        return self._plan_phase_instance

    def _execute(self) -> ExecutePhase:
        if self._execute_phase_instance is None:
            self._execute_phase_instance = ExecutePhase()
        return self._execute_phase_instance

    def _review(self) -> ReviewPhase:
        if self._review_phase_instance is None:
            self._review_phase_instance = ReviewPhase()
        return self._review_phase_instance

    def _fix(self) -> ReviewFixPass:
        if self._review_fix_pass_instance is None:
            self._review_fix_pass_instance = ReviewFixPass()
        return self._review_fix_pass_instance

    def _notify(self) -> Notifier:
        if self._notifier_instance is None:
            self._notifier_instance = Notifier()
        return self._notifier_instance

    # ---- run ------------------------------------------------------------ #
    @workflow.run
    async def run(self, inp: DevLoopInput) -> DevLoopResult:
        started = workflow.now()
        self._devloop_input = inp  # store for activity adapters (#188)

        pipeline = PhasePipeline()
        return await pipeline.run(
            inp,
            # Plan phase
            plan_phase=self._plan_phase_adapter,
            # Execute phase
            execute_phase=self._execute_phase_adapter,
            # Review phase
            review_phase=self._review_phase_adapter,
            # Fix pass
            fix_pass=self._fix_pass_adapter,
            # Notifier
            notifier=self._notify_adapter,
            # Post-round KPI emission callback
            post_round=lambda issue, exec_result, fix_passes, verdict: (
                self._emit_kpis_round(
                    inp, issue, exec_result, fix_passes, verdict, started
                )
            ),
            next_issue=self._dequeue_issue,
        )

    # ---- PhasePipeline adapter methods (pass self as PhaseOps) ---------- #
    async def _plan_phase_adapter(self, inp: DevLoopInput, rnd: int) -> dict | None:
        """Adapter that wires plan_phase callbacks through the workflow's PhaseOps.

        Re-binds ``drop_issues_in_review`` with a closure over the current
        ``inp`` before running the plan phase.
        """
        self.drop_issues_in_review = lambda _inp, issues: self._drop_issues_in_review(
            inp, issues
        )
        return await self._plan().run(inp, rnd, self)

    async def _execute_phase_adapter(self, inp: DevLoopInput, issue: dict) -> dict:
        """Adapter that wires execute_phase callbacks through the workflow's PhaseOps."""
        return await self._execute().run(inp, issue, self)

    async def _review_phase_adapter(
        self, inp: DevLoopInput, issue: dict, exec_result: dict
    ) -> dict | None:
        """Adapter that wires review_phase callbacks through the workflow's PhaseOps."""
        return await self._review().run(inp, issue, exec_result, self)

    async def _fix_pass_adapter(
        self, inp: DevLoopInput, issue: dict, exec_result: dict, review: dict
    ) -> bool:
        """Adapter that wires fix_pass callbacks through the workflow's PhaseOps."""
        return await self._fix().run(inp, issue, exec_result, review, self)

    async def _notify_adapter(
        self, inp: DevLoopInput, issue: dict, exec_result: dict
    ) -> None:
        """Adapter that wires notifier callbacks through the workflow's PhaseOps."""
        await self._notify().run(inp, issue, exec_result, self)

    # ---- KPI emission after each round ---------------------------------- #
    async def _emit_kpis_round(
        self,
        inp: DevLoopInput,
        issue: dict,
        exec_result: dict,
        fix_passes: int,
        verdict: str,
        started: Any,
    ) -> None:
        """Emit per-issue KPIs after a successful round."""
        counters = self._kpi_take()
        await self._emit_kpis(
            WorkflowKpiInput(
                project_id=inp.project_id,
                issue_number=_as_int(issue.get("id")),
                ci_fix_iterations=counters.get("ci_fix_iterations", 0),
                review_fix_passes=fix_passes,
                answer_jobs=counters.get("answer_jobs", 0),
                execute_attempts=counters.get("execute_attempts", 0),
                review_verdict=verdict or "",
                label_to_pr_seconds=(workflow.now() - started).total_seconds(),
                pr_opened=bool(exec_result.get("pr_url")),
                commits=_as_int(exec_result.get("commits")),
                ci_exhausted=bool(exec_result.get("exhausted")),
            )
        )

    # ---- Activity-level adapters (called by phase callbacks) ------------- #
    async def _cleanup_activity(self, job_name: str) -> None:
        """Real ``cleanup_configmap`` activity — adapter for PhaseOps.cleanup."""
        return await self._cleanup(job_name)

    async def _dispatch_activity(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_number: int = 0,
        poll_interval_seconds: float = 5.0,
    ) -> AgentJobResult:
        """Real ``dispatch_agent_job`` activity — adapter for PhaseOps.dispatch."""
        return await self._dispatch(
            project_id,
            spec,
            issue_number=issue_number,
            poll_interval_seconds=poll_interval_seconds,
        )

    async def _kpi_bump_activity(self, key: str, val: int) -> None:
        """Real KPI bump — adapter for PhaseOps.kpi_bump."""
        return self._kpi_bump(key, val)

    async def _kpi_take_activity(self) -> dict:
        """Real KPI take — adapter for PhaseOps.kpi_take."""
        return self._kpi_take()

    async def _emit_kpis_activity(self, inp: WorkflowKpiInput) -> None:
        """Real ``emit_workflow_kpis`` activity — adapter for PhaseOps.emit_kpis."""
        return await self._emit_kpis(inp)

    async def _poll_ci_activity(
        self, project_id: str, pr_number: int
    ) -> CIChecksResult:
        """Real ``poll_ci_checks`` activity — adapter for PhaseOps.poll_ci."""
        return await workflow.execute_activity(
            "poll_ci_checks",
            PollCIChecksInput(project_id=project_id, pr_number=pr_number),
            result_type=CIChecksResult,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _comment_activity(
        self, project_id: str, issue_number: int, body: str
    ) -> None:
        """Real ``post_github_comment`` activity — adapter for PhaseOps.comment."""
        return await self._comment(project_id, issue_number, body)

    async def _plan_issue_activity(self, inp: PlanIssueInput) -> dict:
        """Real ``plan_issue`` activity call — adapter for PlanPhase."""
        return await workflow.execute_activity(
            "plan_issue",
            inp,
            result_type=dict,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_RETRY,
        )

    async def _dispatch_plan_activity(
        self,
        project_id: str,
        spec: TaskSpec,
        poll_interval_seconds: float,
    ) -> AgentJobResult:
        """Real ``dispatch_agent_job`` for plan — adapter for PlanPhase."""
        return await self._dispatch(
            project_id, spec, poll_interval_seconds=poll_interval_seconds
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

    async def _dispatch_execute_activity(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_number: int,
        poll_interval_seconds: float,
    ) -> AgentJobResult:
        """Real dispatch activity — adapter for ExecutePhase."""
        return await self._dispatch(
            project_id,
            spec,
            issue_number=issue_number,
            poll_interval_seconds=poll_interval_seconds,
        )

    async def _answer_question_activity(
        self, project_id: str, issue_no: int, result: AgentJobResult
    ) -> AgentJobResult:
        """Resolve mid-run ``AWAITING_HUMAN`` pauses via the answer agent.

        Each question dispatches a fresh ``Phase.ANSWER`` Agent Execution Job
        that investigates the question; its answer is patched back into the
        paused job's ConfigMap and the original job resumes.

        Once ``max_questions_per_phase`` questions have been dispatched,
        the workflow stops asking and tells the parked job to proceed with its
        best guess (no further answer jobs spawned).
        """
        # This method is called in a loop by the ExecutePhase callbacks.
        # Each call resolves one AWAITING_HUMAN pause.
        inp = self._devloop_input  # stored in ``run`` (#188)
        questions_asked = getattr(self, "_answer_questions_count", 0)
        question = result.question or "unspecified question"
        if questions_asked >= inp.max_questions_per_phase:
            answer = "proceed with your best guess"
            await self._comment(
                project_id,
                issue_no,
                "⚠️ Question limit reached — agent proceeding with best "
                f"guess. Question was: {question}",
            )
        else:
            questions_asked += 1
            self._answer_questions_count = questions_asked  # type: ignore[attr-defined]
            self._kpi_bump("answer_jobs")
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
        await self._cleanup(result.job_name)
        return result

    async def _comment_activity(
        self, project_id: str, issue_number: int, body: str
    ) -> None:
        """Real ``post_github_comment`` activity — adapter for comment calls."""
        return await self._comment(project_id, issue_number, body)

    async def _kpi_bump_activity(self, key: str, val: int) -> None:
        """Real KPI bump — adapter for KPI calls."""
        return self._kpi_bump(key, val)

    async def _dispatch_review_activity(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_no: int,
        poll_interval_seconds: float,
    ) -> AgentJobResult:
        """Real ``dispatch_agent_job`` for review — adapter for ReviewPhase."""
        return await self._dispatch(
            project_id,
            spec,
            issue_number=issue_no,
            poll_interval_seconds=poll_interval_seconds,
        )

    async def _post_review_findings_activity(
        self, project_id: str, pr_url: str, review: dict, result: AgentJobResult
    ) -> None:
        """Post the reviewer's findings to the PR (adapter for ReviewPhase).

        Raises ``RuntimeError`` when findings exist but the PR URL cannot be
        resolved (unparseable or missing), so the failure surfaces rather than
        silently dropping review comments.
        """
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
        pr_number = pr_number_from_url(pr_url)
        if not pr_number:
            raise RuntimeError(
                f"cannot post review findings: pr_url '{pr_url}' "
                f"for project {project_id} is unparseable or missing"
            )
        await workflow.execute_activity(
            "post_pr_comments",
            PostCommentsInput(project_id, pr_number, summary, inline),
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_RETRY,
        )

    async def _dispatch_fix_activity(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_number: int,
        poll_interval_seconds: float,
    ) -> int:
        """Real ``dispatch_agent_job`` for fix pass — adapter for CICycle.

        Returns the commit count extracted from the ``AgentJobResult`` so that
        CICycle can report it back to the workflow (#188).
        """
        result = await self._dispatch(
            project_id,
            spec,
            issue_number=issue_number,
            poll_interval_seconds=poll_interval_seconds,
        )
        return result.commits or 0

    async def _request_reviewer_activity(
        self, project_id: str, pr_number: Optional[int]
    ) -> ReviewerRequestResult:
        """Real ``request_github_reviewer`` activity — adapter for Notifier."""
        return await self._request_reviewer(project_id, pr_number or 0)

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
