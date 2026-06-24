"""ExecutePhase — dispatch the agent execute job.

Wraps the existing ``_execute_phase`` activity call from ``DevLoopWorkflow``
as a standalone deep module with a small interface: ``run(inp, issue, callbacks)``.

Handles dispatch retries for zero commits, mid-run question resolution,
and delegates to ``CICycle`` for the CI fix loop.
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine, Optional

from ..phases.cycle import CICycle
from ..phases.execute_phase_ops import ExecutePhaseOps
from ..phases.phase_ops import PhaseOps
from ..projects import get_project
from ..shared import (
    AgentJobResult,
    JobStatus,
    TaskSpec,
)


# Type aliases for injectable callbacks.
_DispatchExecuteCallback = Callable[
    [str, TaskSpec, int, float], Coroutine[Any, Any, AgentJobResult]
]
_AnswerQuestionCallback = Callable[
    [str, int, AgentJobResult], Coroutine[Any, Any, AgentJobResult]
]
_PostCommentCallback = Callable[[str, int, str], Coroutine[Any, Any, None]]
_KpiBumpCallback = Callable[[str, int], Coroutine[Any, Any, None]]


class ExecutePhase:
    """Dispatch the Execute Agent Execution Job.

    Stateless — all context flows through ``run`` parameters.
    """

    async def run(
        self,
        inp: Any,  # DevLoopInput
        issue: dict,
        callbacks: Optional[PhaseOps] = None,
    ) -> dict:
        """Dispatch the execute job, retrying on zero commits.

        Parameters
        ----------
        inp : DevLoopInput
            Workflow input (must have ``project_id``, ``execute_max_iterations``,
            ``poll_interval_seconds``).
        issue : dict
            Plan issue dict (must have ``id``, ``title``, ``branch``).
        callbacks : PhaseOps, optional
            Injected callbacks for testing.

        Returns
        -------
        dict
            exec_result dict with ``issue_id``, ``branch``, ``pr_url``,
            ``commits``, ``exhausted`` keys.
        """
        cb = callbacks or PhaseOps()
        ops = PhaseOps()
        issue_no = ops.as_int(issue.get("id"))

        try:
            project_cfg = get_project(inp.project_id)
        except KeyError:
            project_cfg = None

        extra: dict = {}
        if project_cfg is not None:
            extra["open_pr_as_draft"] = project_cfg.open_pr_as_draft

        spec = TaskSpec(
            phase="execute",
            project_id=inp.project_id,
            issue_number=issue_no,
            title=issue.get("title", ""),
            branch=issue.get("branch", ""),
            extra=extra,
        )

        max_iters = inp.execute_max_iterations
        result = None
        for attempt in range(1, max_iters + 1):
            exec_ops = cb.execute_ops
            if exec_ops.kpi_bump is not None:
                await exec_ops.kpi_bump("execute_attempts", 1)
            elif cb.kpi_bump is not None:
                await cb.kpi_bump("execute_attempts", 1)
            await ops._phase_comment(
                inp.project_id,
                issue_no,
                "⏳ queued — agent is working on this issue",
                callback=exec_ops.comment or cb.comment or cb.post_comment,
            )
            result = await ops.dispatch_helper(
                inp.project_id,
                spec,
                issue_number=issue_no,
                poll_interval_seconds=inp.poll_interval_seconds,
                dispatch_callback=exec_ops.dispatch_execute or cb.dispatch_execute,
            )
            # Resolve mid-run AWAITING_HUMAN questions.
            result = await self._answer_questions(
                inp.project_id, issue_no, result, cb, exec_ops
            )

            if result.status != JobStatus.COMPLETE.value or result.commits:
                break

        if result is None:
            # execute_max_iterations was misconfigured to < 1, so the loop
            # above never ran — treat it the same as a failed attempt rather
            # than crashing on a None dereference below.
            await ops._phase_comment(
                inp.project_id,
                issue_no,
                "❌ Parked — execute phase failed: execute_max_iterations must be >= 1",
                callback=exec_ops.comment or cb.comment or cb.post_comment,
            )
            return {
                "issue_id": issue_no,
                "branch": "",
                "pr_url": "",
                "commits": 0,
                "exhausted": False,
            }

        if result.status != JobStatus.COMPLETE.value:
            await ops._phase_comment(
                inp.project_id,
                issue_no,
                f"❌ Parked — execute phase failed: {result.error or 'unknown error'}",
                callback=exec_ops.comment or cb.comment or cb.post_comment,
            )
            return {
                "issue_id": issue_no,
                "branch": "",
                "pr_url": "",
                "commits": 0,
                "exhausted": False,
            }

        if not result.commits:
            await ops._phase_comment(
                inp.project_id,
                issue_no,
                f"❌ Execute exhausted {max_iters} attempts with no commits"
                " — skipping this round",
                callback=exec_ops.comment or cb.comment or cb.post_comment,
            )
            return {
                "issue_id": issue_no,
                "branch": "",
                "pr_url": "",
                "commits": 0,
                "exhausted": False,
            }

        await ops._phase_comment(
            inp.project_id,
            issue_no,
            f"✅ Implemented — PR: {result.pr_url or result.branch}",
            callback=exec_ops.comment or cb.comment or cb.post_comment,
        )
        exec_result = {
            "issue_id": issue_no,
            "branch": result.branch,
            "pr_url": result.pr_url,
            "commits": result.commits,
            "exhausted": False,
        }

        # Run CI fix cycle (delegates to standalone CICycle).
        ci_fix_max_iters = getattr(inp, "ci_fix_max_iterations", 5)
        poll_interval = getattr(inp, "poll_interval_seconds", 5.0)
        cycle_result = await CICycle().run(
            project_id=inp.project_id,
            issue_no=issue_no,
            exec_result=exec_result,
            ci_fix_max_iterations=ci_fix_max_iters,
            poll_interval_seconds=poll_interval,
            callbacks=cb.phaseops,
        )
        exec_result["exhausted"] = cycle_result.exhausted
        return exec_result

    async def _answer_questions(
        self,
        project_id: str,
        issue_no: int,
        result: AgentJobResult,
        cb: PhaseOps,
        exec_ops: Optional[ExecutePhaseOps] = None,
    ) -> AgentJobResult:
        """Resolve mid-run AWAITING_HUMAN questions.

        Only dispatches a question resolver when the job is paused
        (AWAITING_HUMAN).  Otherwise the original result flows through
        unchanged — this preserves the zero-commits / parked-path where
        the status is COMPLETE or FAILED but no human question was asked.
        """
        if result.status != JobStatus.AWAITING_HUMAN.value:
            return result
        ops = exec_ops or cb.execute_ops
        if ops.answer_question is not None:
            return await ops.answer_question(project_id, issue_no, result)
        # Default: no question resolution — return result as-is.
        return result
