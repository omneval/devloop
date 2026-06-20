from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from temporalio import workflow

from ._workflow_common import _WorkflowCommon
from .execution import AgentJobResult, TaskSpec
from .github import ReviewerRequestResult
from .phases.cycle import CICycle
from .phases.phase_ops import PhaseOps
from .phases.pr_comment import PRCommentPhase


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


@workflow.defn
class PRCommentWorkflow(_WorkflowCommon, PhaseOps):
    """Respond to reviewer feedback on open agent PRs.

    Thin adapter — composes PRCommentPhase, CICycle, and
    reviewer request as separate deep modules.  Implements the
    PhaseOps protocol by wiring its ``_WorkflowCommon`` helpers as
    callable fields.
    """

    def __init__(self) -> None:
        # PhaseOps adapters (use local ``workflow`` from this module) --------- #
        self.comment = self._comment_activity
        self.dispatch = self._dispatch_activity
        self.request_reviewer = self._request_reviewer_activity
        # Lazy-init: phase instances are workflow state and must survive replay.
        self._pr_comment_phase_instance: Optional[PRCommentPhase] = None

    # ---- PhaseOps adapters ----------------------------------------------- #

    async def _comment_activity(
        self, project_id: str, issue_number: int, body: str
    ) -> None:
        """Real ``post_github_comment`` activity — adapter for PhaseOps.comment."""
        return await self._comment(project_id, issue_number, body)

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

    async def _request_reviewer_activity(
        self, project_id: str, pr_number: int | None
    ) -> ReviewerRequestResult:
        """Real ``request_github_reviewer`` activity — adapter for PhaseOps.request_reviewer."""
        return await self._request_reviewer(project_id, pr_number)

    @workflow.run
    async def run(self, inp: PRCommentInput) -> PRCommentResult:
        # 1. PRCommentPhase: branch resolution, validation, dispatch
        phase = self._pr_comment_phase_instance or PRCommentPhase()
        phase_result = await phase.run(inp)

        if phase_result.error:
            return PRCommentResult(
                status="failed",
                pr_number=inp.pr_number,
                detail=phase_result.error,
            )

        exec_result: dict = phase_result.exec_result or {}
        issue_no = inp.issue_number or inp.pr_number

        # 2. CI Fix Cycle
        cycle_result = await CICycle().run(
            project_id=inp.project_id,
            issue_no=issue_no,
            exec_result=exec_result,
            ci_fix_max_iterations=inp.ci_fix_max_iterations,
            poll_interval_seconds=inp.poll_interval_seconds,
        )

        # 3. Request reviewer
        await self._request_reviewer(inp.project_id, inp.pr_number)

        # 4. Post result comment with summary
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
        await self._comment(inp.project_id, issue_no, body)

        return PRCommentResult(
            status="completed",
            pr_number=inp.pr_number,
            commits=exec_result.get("commits", 0),
            exhausted=cycle_result.exhausted,
        )
