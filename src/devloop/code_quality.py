"""CodeQualityWorkflow — scheduled sentrux scan with AI-driven improvement issue filing (issue #82).

Thin adapter: wires ``ScanningPhase``, ``QualityGate``, and ``ImprovePhase``
together.  All orchestration logic lives in the phase modules; this class only
creates phases, binds its ``PhaseOps`` methods as callbacks, and calls their
``run()`` methods in the correct order.

The workflow handles gate logic that is *between* phases — opening/closing
GitHub issues, posting result comments, and deciding whether to dispatch
improvement work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

from .execution import AgentJobResult, DispatchInput, TaskSpec, WorkflowKpiInput
from .github import (
    GithubNotificationInput,
    PlanIssueInput,
    RequestReviewerInput,
    ReviewerRequestResult,
)
from .phases.phase_ops import PhaseOps

with workflow.unsafe.imports_passed_through():
    from ._constants import _GITHUB_COMMENT_TIMEOUT, _RETRY
    from .phases.improve_phase import ImprovePhase, ImprovePhaseCallbacks
    from .phases.quality_gate import QualityGate
    from .phases.scanning_phase import ScanningPhase, ScanningPhaseCallbacks
    from .shared import (
        CreateGithubIssueInput,
        CIChecksResult,
        UpdateGithubIssueInput,
    )


@dataclass
class CodeQualityInput:
    project_id: str
    threshold: int = 7000  # 0–10000 native sentrux scale
    agent_label: str = "agent-ready"


@workflow.defn
class CodeQualityWorkflow(PhaseOps):
    """Thin adapter: wires phase instances into a code-quality pipeline.

    The workflow itself is the unified callback seam: every phase receives
    ``self`` (a ``PhaseOps``) with all methods wired to Temporal activity
    calls.  The ``run`` method delegates to the three deep phase modules.
    """

    def __init__(self) -> None:
        # Wire every PhaseOps callback field to a Temporal activity adapter.
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
        # Lazy-init: phase instances are workflow state and must survive replay.
        self._scanning_phase_instance: Optional[ScanningPhase] = None
        self._improve_phase_instance: Optional[ImprovePhase] = None

    # ---- Lazy phase constructors ---------------------------------------- #

    def _scanning_phase(self) -> ScanningPhase:
        if self._scanning_phase_instance is None:
            self._scanning_phase_instance = ScanningPhase()
        return self._scanning_phase_instance

    def _improve_phase(self) -> ImprovePhase:
        if self._improve_phase_instance is None:
            self._improve_phase_instance = ImprovePhase()
        return self._improve_phase_instance

    # ---- PhaseOps adapters ----------------------------------------------- #

    async def _comment(self, project_id: str, issue_number: int, body: str) -> None:
        """Delegate to PhaseOps._comment so code-quality code paths
        exercise the injectable callback protocol."""
        return await PhaseOps._comment(self, project_id, issue_number, body)

    async def _dispatch(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_number: int = 0,
        poll_interval_seconds: float = 5.0,
    ) -> AgentJobResult:
        """Delegate to PhaseOps._dispatch so code-quality code paths
        exercise the injectable callback protocol."""
        return await PhaseOps._dispatch(
            self, project_id, spec, issue_number, poll_interval_seconds
        )

    async def _cleanup(self, job_name: str) -> None:
        """Delegate to PhaseOps._cleanup so code-quality code paths
        exercise the injectable callback protocol."""
        return await PhaseOps._cleanup(self, job_name)

    async def _request_reviewer(
        self, project_id: str, pr_number: int | None
    ) -> ReviewerRequestResult:
        """Delegate to PhaseOps._request_reviewer so code-quality code paths
        exercise the injectable callback protocol."""
        return await PhaseOps._request_reviewer(self, project_id, pr_number)

    async def _emit_kpis(self, inp: WorkflowKpiInput) -> None:
        """Delegate to PhaseOps._emit_kpis so code-quality code paths
        exercise the injectable callback protocol."""
        return await PhaseOps._emit_kpis(self, inp)

    # ---- Temporal activity adapters -------------------------------------- #

    async def _comment_activity(
        self, project_id: str, issue_number: int, body: str
    ) -> None:
        """Real ``post_github_comment`` activity — adapter for PhaseOps.comment."""
        return await workflow.execute_activity(
            "post_github_comment",
            GithubNotificationInput(
                issue_number=issue_number,
                project_id=project_id,
                body=body,
            ),
            start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _cleanup_activity(self, job_name: str) -> None:
        """Real ``cleanup_configmap`` activity — adapter for PhaseOps.cleanup."""
        return await workflow.execute_activity(
            "cleanup_configmap",
            job_name,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

    async def _dispatch_activity(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_number: int,
        poll_interval_seconds: float,
    ) -> AgentJobResult:
        """Real ``dispatch_agent_job`` activity — adapter for PhaseOps.dispatch."""
        from ._constants import JOB_DISPATCH_QUEUE

        return await workflow.execute_activity(
            "dispatch_agent_job",
            DispatchInput(
                project_id,
                issue_number,
                spec,
                poll_interval_seconds=poll_interval_seconds,
            ),
            result_type=AgentJobResult,
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=_RETRY,
            task_queue=JOB_DISPATCH_QUEUE,
        )

    async def _kpi_bump_activity(self, key: str, val: int, n: int = 1) -> None:
        """Real KPI bump — adapter for PhaseOps.kpi_bump."""
        return self._kpi_bump(key, val)

    async def _kpi_take_activity(self) -> dict:
        """Real KPI take — adapter for PhaseOps.kpi_take."""
        return self._kpi_take()

    async def _emit_kpis_activity(self, inp: WorkflowKpiInput) -> None:
        """Real ``emit_workflow_kpis`` activity — adapter for PhaseOps.emit_kpis."""
        try:
            await workflow.execute_activity(
                "emit_workflow_kpis",
                inp,
                start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
                retry_policy=_RETRY,
            )
        except Exception:  # noqa: BLE001
            workflow.logger.warning("emit_workflow_kpis failed (ignored)")

    async def _poll_ci_activity(
        self, project_id: str, pr_number: int
    ) -> CIChecksResult:
        """Real ``poll_ci_checks`` activity — adapter for PhaseOps.poll_ci."""
        from .cichecks import PollCIChecksInput

        return await workflow.execute_activity(
            "poll_ci_checks",
            PollCIChecksInput(project_id=project_id, pr_number=pr_number),
            result_type=CIChecksResult,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_RETRY,
        )

    async def _request_reviewer_activity(
        self, project_id: str, pr_number: int | None
    ) -> ReviewerRequestResult:
        """Real ``request_github_reviewer`` activity — adapter for PhaseOps.request_reviewer."""
        return await workflow.execute_activity(
            "request_github_reviewer",
            RequestReviewerInput(
                project_id=project_id,
                pr_number=pr_number,
                reviewer="",
            ),
            result_type=ReviewerRequestResult,
            start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _dispatch_execute_activity(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_number: int,
        poll_interval_seconds: float,
    ) -> AgentJobResult:
        """Real ``dispatch_agent_job`` activity — adapter for PhaseOps.dispatch_execute."""
        from ._constants import JOB_DISPATCH_QUEUE

        return await workflow.execute_activity(
            "dispatch_agent_job",
            DispatchInput(
                project_id,
                issue_number,
                spec,
                poll_interval_seconds=poll_interval_seconds,
            ),
            result_type=AgentJobResult,
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=_RETRY,
            task_queue=JOB_DISPATCH_QUEUE,
        )

    async def _answer_question_activity(
        self, project_id: str, issue_number: int, job_result: AgentJobResult
    ) -> AgentJobResult:
        """Real answer_question activity — adapter for PhaseOps.answer_question."""
        from ._constants import _ACTIVITY_TIMEOUT

        return await workflow.execute_activity(
            "answer_question",
            (project_id, issue_number, job_result),
            result_type=AgentJobResult,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _dispatch_review_activity(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_number: int,
        poll_interval_seconds: float,
    ) -> AgentJobResult:
        """Real ``dispatch_agent_job`` activity — adapter for PhaseOps.dispatch_review."""
        from ._constants import JOB_DISPATCH_QUEUE

        return await workflow.execute_activity(
            "dispatch_agent_job",
            DispatchInput(
                project_id,
                issue_number,
                spec,
                poll_interval_seconds=poll_interval_seconds,
            ),
            result_type=AgentJobResult,
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=_RETRY,
            task_queue=JOB_DISPATCH_QUEUE,
        )

    async def _post_review_findings_activity(
        self,
        project_id: str,
        pr_url: str,
        review: dict,
        result: AgentJobResult,
    ) -> None:
        """Real post_review_findings activity — adapter for PhaseOps.post_review_findings."""
        from ._constants import _ACTIVITY_TIMEOUT

        await workflow.execute_activity(
            "post_review_findings",
            (project_id, pr_url, review, result),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _dispatch_fix_activity(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_number: int,
        poll_interval_seconds: float,
    ) -> int:
        """Real ``dispatch_agent_job`` activity — adapter for PhaseOps.dispatch_fix."""
        from ._constants import JOB_DISPATCH_QUEUE

        result = await workflow.execute_activity(
            "dispatch_agent_job",
            DispatchInput(
                project_id,
                issue_number,
                spec,
                poll_interval_seconds=poll_interval_seconds,
            ),
            result_type=AgentJobResult,
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=_RETRY,
            task_queue=JOB_DISPATCH_QUEUE,
        )
        return result.commits or 0

    async def _plan_issue_activity(self, inp: PlanIssueInput) -> dict:
        """Real ``plan_issue`` activity — adapter for PhaseOps.plan_issue."""
        from ._constants import _ACTIVITY_TIMEOUT

        return await workflow.execute_activity(
            "plan_issue",
            inp,
            result_type=dict,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _dispatch_plan_activity(
        self,
        project_id: str,
        spec: TaskSpec,
        poll_interval_seconds: float,
    ) -> AgentJobResult:
        """Real ``dispatch_agent_job`` activity — adapter for PhaseOps.dispatch_plan."""
        from ._constants import JOB_DISPATCH_QUEUE

        return await workflow.execute_activity(
            "dispatch_agent_job",
            DispatchInput(
                project_id,
                0,
                spec,
                poll_interval_seconds=poll_interval_seconds,
            ),
            result_type=AgentJobResult,
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=_RETRY,
            task_queue=JOB_DISPATCH_QUEUE,
        )

    async def _drop_issues_in_review(
        self, issues: list[dict], open_prs: list[dict]
    ) -> list[dict]:
        """Real ``drop_issues_in_review`` activity — adapter for PhaseOps.drop_issues_in_review."""
        from ._constants import _ACTIVITY_TIMEOUT

        return await workflow.execute_activity(
            "drop_issues_in_review",
            (issues, open_prs),
            result_type=list,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

    # ---- Callback methods bound to PhaseOps helpers -------------------- #

    async def _cb_post_comment(
        self, project_id: str, issue_number: int, body: str
    ) -> None:
        """Real ``post_github_comment`` activity — adapter for comment calls."""
        return await self._comment(project_id, issue_number, body)

    async def _cb_dispatch(
        self, project_id: str, spec: TaskSpec, issue_number: int, poll: float
    ) -> AgentJobResult:
        """Real ``dispatch_agent_job`` activity — adapter for dispatch calls."""
        return await self._dispatch(
            project_id,
            spec,
            issue_number=issue_number,
            poll_interval_seconds=poll,
        )

    # ---- Public workflow entry point ----------------------------------- #

    @workflow.run
    async def run(self, inp: CodeQualityInput) -> None:
        """Run the code-quality pipeline.

        1. Open parent issue and post queued comment via ``ScanningPhase``.
        2. Evaluate the quality gate (pure ``QualityGate``).
        3. Update the parent issue and post result comments.
        4. On gate failure, dispatch improvement work via ``ImprovePhase``.
        """
        project_id = inp.project_id
        threshold = inp.threshold
        agent_label = inp.agent_label

        # ── Phase 1: Scan ──────────────────────────────────────────────────
        scan_callbacks = ScanningPhaseCallbacks.default()
        scan_callbacks.create_issue = self._create_issue_activity
        scan_callbacks.dispatch = self._cb_dispatch
        scan_callbacks.post_comment = self._cb_post_comment

        scan_result = await self._scanning_phase().run(
            project_id=project_id,
            threshold=threshold,
            callbacks=scan_callbacks,
        )

        score = scan_result["score"]
        report = scan_result["report"]
        scan_error = scan_result["scan_error"]
        error_message = scan_result["error_message"]
        parent_issue_number = scan_result["parent_issue_number"]

        # ── Phase 2: Quality Gate ──────────────────────────────────────────
        decision = QualityGate.check(score, threshold)

        if scan_error:
            # Abort path — scan error (no rules.toml etc.)
            error_body = f"## Scan Error\n\n{error_message or report}"
            await workflow.execute_activity(
                "update_github_issue",
                UpdateGithubIssueInput(
                    project_id=project_id,
                    issue_number=parent_issue_number,
                    body=error_body,
                    state="closed",
                ),
                start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
                retry_policy=_RETRY,
            )
            return

        result_body = (
            f"## Code Quality Report\n\n"
            f"Score: **{score}** / 10000 (threshold: {threshold})\n\n"
            f"```\n{report}\n```"
        )

        if decision == "pass":
            # Pass path — score meets threshold
            await workflow.execute_activity(
                "update_github_issue",
                UpdateGithubIssueInput(
                    project_id=project_id,
                    issue_number=parent_issue_number,
                    body=result_body,
                ),
                start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
                retry_policy=_RETRY,
            )
            await self._comment(
                project_id,
                parent_issue_number,
                f"✅ Quality check passed: {score}/{threshold}",
            )
            await workflow.execute_activity(
                "update_github_issue",
                UpdateGithubIssueInput(
                    project_id=project_id,
                    issue_number=parent_issue_number,
                    state="closed",
                ),
                start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
                retry_policy=_RETRY,
            )
            return

        # ── Phase 3: Improve (fail path) ───────────────────────────────────
        # Fail path — score below threshold
        await workflow.execute_activity(
            "update_github_issue",
            UpdateGithubIssueInput(
                project_id=project_id,
                issue_number=parent_issue_number,
                body=result_body,
            ),
            start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
            retry_policy=_RETRY,
        )
        await self._comment(
            project_id,
            parent_issue_number,
            f"⚠️ Quality below threshold ({score} < {threshold}) — filing improvement issues.",
        )

        improve_callbacks = ImprovePhaseCallbacks.default()
        improve_callbacks.dispatch = self._cb_dispatch
        improve_callbacks.post_comment = self._cb_post_comment

        await self._improve_phase().run(
            project_id=project_id,
            report=report,
            parent_issue_number=parent_issue_number,
            agent_label=agent_label,
            callbacks=improve_callbacks,
        )

    # ── Activity adapters used by phases ─────────────────────────────── #

    async def _create_issue_activity(self, inp: CreateGithubIssueInput) -> int:
        """Create a parent GitHub issue (adapter for ScanningPhase)."""
        return await workflow.execute_activity(
            "create_github_issue",
            inp,
            result_type=int,
            start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
            retry_policy=_RETRY,
        )
