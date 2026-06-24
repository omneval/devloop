from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

from .execution import (
    AgentJobResult,
    DispatchInput,
    TaskSpec,
    WorkflowKpiInput,
)
from .github import (
    GithubNotificationInput,
    RequestReviewerInput,
    ReviewerRequestResult,
)
from .phases.cycle import CICycle
from .phases.notifier import Notifier
from .phases.phase_ops import PhaseOps
from .phases.pr_comment import (
    PRCommentPhase,
    PRCommentPhaseCallbacks as _PhaseCallbacks,
)
from ._constants import JOB_DISPATCH_QUEUE, _ACTIVITY_TIMEOUT


@dataclass
class PRCommentInput:
    project_id: str
    pr_number: int
    issue_number: int = 0
    branch: str = ""
    # the human's feedback: a review body (pull_request_review) or a comment
    # body (issue_comment) — whichever triggered this run
    comment_body: str = ""
    # "review" | "comment" — which kind of feedback triggered this run
    source: str = "comment"
    author: str = ""
    poll_interval_seconds: float = 5.0
    ci_fix_max_iterations: int = 5

    @classmethod
    def from_env(
        cls,
        project_id: str,
        pr_number: int,
        issue_number: int,
        branch: str,
        comment_body: str,
        source: str,
        author: str,
    ) -> "PRCommentInput":
        """Build an input with the timeout gates sourced from the worker env —
        same lazy-resolution pattern as DevLoopInput.from_env: called only
        from the webhook entry point (outside the workflow sandbox)."""
        import os

        def _int(name: str, default: int) -> int:
            try:
                return int(os.environ[name])
            except (KeyError, ValueError):
                return default

        return cls(
            project_id=project_id,
            pr_number=pr_number,
            issue_number=issue_number,
            branch=branch,
            comment_body=comment_body,
            source=source,
            author=author,
            ci_fix_max_iterations=_int(
                "CI_FIX_MAX_ITERATIONS", cls.ci_fix_max_iterations
            ),
        )


@dataclass
class PRCommentResult:
    status: str  # completed | failed
    pr_number: int = 0
    commits: int = 0
    exhausted: bool = False
    detail: str = ""
    exec_result: dict | None = None
    error: str | None = None


@workflow.defn
class PRCommentWorkflow(PhaseOps):
    """Respond to reviewer feedback on open agent PRs.

    Thin adapter — composes PRCommentPhase, CICycle, and Notifier as
    deep phases with injectable callbacks.  All orchestration lives in
    ``run``; the body wires phase instances, binds its own ``PhaseOps``
    methods as callbacks, and delegates each phase's ``run()``.

    The 5 phases are wired with injectable callbacks consistent with the
    PhaseOps pattern (issues #187/#188):

    1. **PRCommentPhase** — branch resolution, validation, task dispatch
    2. **CICycle** — CI fix loop (poll, dispatch-fix, re-poll)
    3. **Notifier** — reviewer request + notification comment
    4. **Comment** — post the result summary (adapter method)
    5. **Status assembly** — final status callback
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
            request_reviewer=self._request_reviewer_activity,
        )
        # Lazy-init: phase instances are workflow state and must survive replay.
        self._pr_comment_phase_instance: Optional[PRCommentPhase] = None
        self._cycle_instance: Optional[CICycle] = None
        self._notifier_instance: Optional[Notifier] = None

    # ---- lazy phase constructors ---------------------------------------- #

    def _pr_comment_phase(self) -> PRCommentPhase:
        if self._pr_comment_phase_instance is None:
            self._pr_comment_phase_instance = PRCommentPhase()
        return self._pr_comment_phase_instance

    def _cycle(self) -> CICycle:
        if self._cycle_instance is None:
            self._cycle_instance = CICycle()
        return self._cycle_instance

    def _notifier(self) -> Notifier:
        if self._notifier_instance is None:
            self._notifier_instance = Notifier()
        return self._notifier_instance

    # ---- PhaseOps adapters ----------------------------------------------- #

    async def _comment(
        self,
        project_id: str,
        issue_number: int,
        body: str,
    ) -> None:
        """Delegate to PhaseOps._comment so PRCommentWorkflow code paths
        exercise the injectable callback protocol."""
        return await PhaseOps._comment(self, project_id, issue_number, body)

    async def _dispatch(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_number: int = 0,
        poll_interval_seconds: float = 5.0,
    ) -> AgentJobResult:
        """Delegate to PhaseOps._dispatch so PRCommentWorkflow code paths
        exercise the injectable callback protocol."""
        return await PhaseOps._dispatch(
            self, project_id, spec, issue_number, poll_interval_seconds
        )

    async def _cleanup(self, job_name: str) -> None:
        """Delegate to PhaseOps._cleanup so PRCommentWorkflow code paths
        exercise the injectable callback protocol."""
        return await PhaseOps._cleanup(self, job_name)

    async def _request_reviewer(
        self,
        project_id: str,
        pr_number: int | None,
    ) -> ReviewerRequestResult:
        """Delegate to PhaseOps._request_reviewer so PRCommentWorkflow code
        paths exercise the injectable callback protocol."""
        return await PhaseOps._request_reviewer(self, project_id, pr_number)

    async def _emit_kpis(
        self,
        inp: "WorkflowKpiInput",
    ) -> None:
        """Delegate to PhaseOps._emit_kpis so PRCommentWorkflow code paths
        exercise the injectable callback protocol."""
        return await PhaseOps._emit_kpis(self, inp)

    async def _comment_activity(
        self, project_id: str, issue_number: int, body: str
    ) -> None:
        """Real ``post_github_comment`` activity — adapter for PhaseOps.comment.

        Called from within PhaseOps._comment when ``self.comment`` callback is
        set (which it is, in ``__init__``).
        """
        return await workflow.execute_activity(
            "post_github_comment",
            GithubNotificationInput(
                issue_number=issue_number,
                project_id=project_id,
                body=body,
            ),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

    async def _dispatch_activity(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_number: int = 0,
        poll_interval_seconds: float = 5.0,
    ) -> AgentJobResult:
        """Real ``dispatch_agent_job`` activity — adapter for PhaseOps.dispatch.

        Called from within PhaseOps._dispatch when ``self.dispatch`` callback is
        set (which it is, in ``__init__``).
        """
        return await workflow.execute_activity(
            "dispatch_agent_job",
            DispatchInput(
                project_id,
                issue_number,
                spec,
                poll_interval_seconds=poll_interval_seconds,
            ),
            result_type=AgentJobResult,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=3),
            task_queue=JOB_DISPATCH_QUEUE,
        )

    async def _request_reviewer_activity(
        self, project_id: str, pr_number: int | None
    ) -> ReviewerRequestResult:
        """Real ``request_github_reviewer`` activity — adapter for PhaseOps.request_reviewer.

        Called from within PhaseOps._request_reviewer when ``self.request_reviewer``
        callback is set (which it is, in ``__init__``).
        """
        return await workflow.execute_activity(
            "request_github_reviewer",
            RequestReviewerInput(
                project_id=project_id,
                pr_number=pr_number,
                reviewer="",
            ),
            result_type=ReviewerRequestResult,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

    async def _cleanup_activity(self, job_name: str) -> None:
        """Real ``cleanup_configmap`` activity — adapter for PhaseOps.cleanup.

        Called from within PhaseOps._cleanup when ``self.cleanup`` callback is
        set (which it is, in ``__init__``).
        """
        if not job_name:
            return
        try:
            await workflow.execute_activity(
                "cleanup_configmap",
                job_name,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except Exception:  # noqa: BLE001
            workflow.logger.warning("cleanup_configmap failed for %s", job_name)

    # ---- KPI counter methods (local state, not I/O) ----------------------- #

    def _kpi_bump(self, key: str, n: int = 1) -> None:
        """Increment a per-issue KPI counter."""
        counters = getattr(self, "_kpi_counters", None)
        if counters is None:
            counters = {}
            self._kpi_counters = counters
        counters[key] = counters.get(key, 0) + n

    def _kpi_take(self) -> dict:
        """Return and reset the accumulated counters (one issue's worth)."""
        counters = getattr(self, "_kpi_counters", None) or {}
        self._kpi_counters = {}
        return counters

    async def _kpi_bump_activity(self, key: str, val: int) -> None:
        """Real KPI bump — adapter for PhaseOps.kpi_bump."""
        return self._kpi_bump(key, val)

    async def _kpi_take_activity(self) -> dict:
        """Real KPI take — adapter for PhaseOps.kpi_take."""
        return self._kpi_take()

    async def _emit_kpis_activity(self, inp: WorkflowKpiInput) -> None:
        """Real ``emit_workflow_kpis`` activity — adapter for PhaseOps.emit_kpis.

        Calls ``workflow.execute_activity`` directly to avoid infinite recursion
        through the PhaseOps delegation layer.
        """
        return await workflow.execute_activity(
            "emit_workflow_kpis",
            inp,
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

    @workflow.run
    async def run(self, inp: PRCommentInput) -> PRCommentResult:
        # 1. PRCommentPhase: branch resolution, validation, dispatch
        phase_result = await self._pr_comment_phase_adapter(inp)

        if phase_result.error:
            return PRCommentResult(
                status="failed",
                pr_number=inp.pr_number,
                detail=phase_result.error,
            )

        exec_result: dict = phase_result.exec_result or {}
        issue_no = inp.issue_number or inp.pr_number

        # 2. CICycle: CI fix loop
        cycle_result = await self._cycle_adapter(inp, issue_no, exec_result)

        # 3. Notifier: reviewer request + notification
        await self._notifier_adapter(inp, issue_no, exec_result, cycle_result)

        # 4. Comment: post the result summary
        note = (
            " ⚠️ CI is still failing after exhausting the CI fix attempts —"
            " please take another look."
            if cycle_result.exhausted
            else ""
        )
        summary = (exec_result.get("summary") or "").strip()
        body = f"👀 Addressed your feedback on PR #{inp.pr_number}.{note}"
        if summary:
            body += f"\n\n{summary}"
        await self._cb_post_comment(inp.project_id, issue_no, body)

        # 5. Status assembly: final status callback
        return await self._status_assembly_adapter(inp, exec_result, cycle_result)

    # ---- Phase adapter methods ------------------------------------------ #

    async def _pr_comment_phase_adapter(self, inp: PRCommentInput) -> PRCommentResult:
        """Adapter that binds PRCommentPhase callbacks."""
        callbacks = _PhaseCallbacks.default()
        callbacks.post_comment = self._cb_post_comment
        callbacks.get_branch = self._cb_get_branch
        callbacks.dispatch = self._cb_dispatch
        return await self._pr_comment_phase().run(inp, callbacks=callbacks)

    async def _cycle_adapter(
        self,
        inp: PRCommentInput,
        issue_no: int,
        exec_result: dict,
    ) -> Any:
        """Adapter that binds CICycle callbacks."""
        callbacks = PhaseOps.default()
        callbacks.poll_ci = self._cb_poll_ci
        callbacks.dispatch_fix = self._cb_dispatch_fix
        callbacks.post_comment = self._cb_post_comment
        callbacks.kpi_bump = self._cb_kpi_bump
        callbacks.cleanup = self._cb_cleanup
        return await self._cycle().run(
            project_id=inp.project_id,
            issue_no=issue_no,
            exec_result=exec_result,
            ci_fix_max_iterations=inp.ci_fix_max_iterations,
            poll_interval_seconds=inp.poll_interval_seconds,
            callbacks=callbacks,
        )

    async def _notifier_adapter(
        self,
        inp: PRCommentInput,
        issue_no: int,
        exec_result: dict,
        cycle_result: Any,
    ) -> None:
        """Adapter that binds Notifier callbacks."""
        callbacks = PhaseOps.default()
        callbacks.request_reviewer = self._cb_request_reviewer
        callbacks.post_comment = self._cb_post_comment
        exec_result_with_exhausted = dict(exec_result)
        exec_result_with_exhausted["exhausted"] = cycle_result.exhausted
        await self._notifier().run(
            inp, {"id": issue_no}, exec_result_with_exhausted, callbacks
        )

    async def _status_assembly_adapter(
        self,
        inp: PRCommentInput,
        exec_result: dict,
        cycle_result: Any,
    ) -> PRCommentResult:
        """Final status callback — assembles and returns the result."""
        return PRCommentResult(
            status="completed",
            pr_number=inp.pr_number,
            commits=exec_result.get("commits", 0),
            exhausted=cycle_result.exhausted,
        )

    # ---- Callback methods bound to PhaseOps helpers -------------------- #

    async def _cb_post_comment(
        self, project_id: str, issue_number: int, body: str
    ) -> None:
        """Real ``post_github_comment`` activity — adapter for comment calls."""
        return await self._comment(project_id, issue_number, body)

    async def _cb_get_branch(self, project_id: str, pr_number: int) -> str:
        """Real ``get_pr_branch`` activity — adapter for PRCommentPhase."""
        from .shared import GetPRBranchInput

        return await workflow.execute_activity(
            "get_pr_branch",
            GetPRBranchInput(project_id=project_id, pr_number=pr_number),
            result_type=str,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

    async def _cb_dispatch(
        self, project_id: str, spec: TaskSpec, issue_number: int, poll: float
    ) -> Any:
        """Real ``dispatch_agent_job`` activity — adapter for PRCommentPhase."""
        return await self._dispatch(
            project_id,
            spec,
            issue_number=issue_number,
            poll_interval_seconds=poll,
        )

    async def _cb_poll_ci(self, project_id: str, pr_number: int) -> Any:
        """Real ``poll_ci_checks`` activity — adapter for CICycle."""
        from .shared import CIChecksResult, PollCIChecksInput

        return await workflow.execute_activity(
            "poll_ci_checks",
            PollCIChecksInput(project_id=project_id, pr_number=pr_number),
            result_type=CIChecksResult,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

    async def _cb_dispatch_fix(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_number: int,
        poll_interval_seconds: float,
    ) -> int:
        """Real ``dispatch_agent_job`` activity — adapter for CICycle."""
        result = await self._dispatch(
            project_id,
            spec,
            issue_number=issue_number,
            poll_interval_seconds=poll_interval_seconds,
        )
        return result.commits or 0

    async def _cb_kpi_bump(self, key: str, val: int) -> None:
        """Real ``_kpi_bump`` helper — adapter for CICycle."""
        self._kpi_bump(key, val)

    async def _cb_cleanup(self, job_name: str) -> None:
        """Real ``cleanup_configmap`` activity — adapter for CICycle."""
        return await self._cleanup(job_name)

    async def _cb_request_reviewer(
        self, project_id: str, pr_number: Optional[int]
    ) -> Any:
        """Real ``request_github_reviewer`` activity — adapter for Notifier."""
        return await self._request_reviewer(project_id, pr_number or 0)
