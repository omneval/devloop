"""ExecutePhase — dispatch the agent execute job.

Wraps the existing ``_execute_phase`` activity call from ``DevLoopWorkflow``
as a standalone deep module with a small interface: ``run(inp, issue, callbacks)``.

Handles dispatch retries for zero commits, mid-run question resolution,
and delegates to ``CICycle`` for the CI fix loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Coroutine, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

from .._constants import _ACTIVITY_TIMEOUT, _GITHUB_COMMENT_TIMEOUT, _RETRY
from ..phases.cycle import CICycle
from ..shared import (
    AgentJobResult,
    DispatchInput,
    GithubNotificationInput,
    JOB_DISPATCH_QUEUE,
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


@dataclass
class ExecutePhaseCallbacks:
    """Callback set for ExecutePhase.run().

    When all fields are ``None``, the default Temporal activity paths are used.
    """

    dispatch_execute: Optional[_DispatchExecuteCallback] = None
    answer_question: Optional[_AnswerQuestionCallback] = None
    post_comment: Optional[_PostCommentCallback] = None
    kpi_bump: Optional[_KpiBumpCallback] = None

    @classmethod
    def default(cls) -> "ExecutePhaseCallbacks":
        """Return a callbacks instance that delegates to Temporal activities."""
        return cls()


class ExecutePhase:
    """Dispatch the Execute Agent Execution Job.

    Stateless — all context flows through ``run`` parameters.
    """

    async def run(
        self,
        inp: Any,  # DevLoopInput
        issue: dict,
        callbacks: Optional[ExecutePhaseCallbacks] = None,
    ) -> dict:
        """Dispatch the execute job, retrying on zero commits.

        Parameters
        ----------
        inp : DevLoopInput
            Workflow input (must have ``project_id``, ``execute_max_iterations``,
            ``poll_interval_seconds``).
        issue : dict
            Plan issue dict (must have ``id``, ``title``, ``branch``).
        callbacks : ExecutePhaseCallbacks, optional
            Injected callbacks for testing.

        Returns
        -------
        dict
            exec_result dict with ``issue_id``, ``branch``, ``pr_url``,
            ``commits``, ``exhausted`` keys.
        """
        cb = callbacks or ExecutePhaseCallbacks.default()
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
            if cb.kpi_bump is not None:
                await cb.kpi_bump("execute_attempts", 1)
            await self._comment(
                inp.project_id,
                issue_no,
                "⏳ queued — agent is working on this issue",
                cb,
            )
            result = await self._dispatch_execute(
                inp.project_id,
                spec,
                issue_number=issue_no,
                poll_interval_seconds=inp.poll_interval_seconds,
                cb=cb,
            )
            # Resolve mid-run AWAITING_HUMAN questions.
            result = await self._answer_questions(inp.project_id, issue_no, result, cb)

            if result.status != JobStatus.COMPLETE.value or result.commits:
                break

        if result.status != JobStatus.COMPLETE.value:
            await self._comment(
                inp.project_id,
                issue_no,
                f"❌ Parked — execute phase failed: {result.error or 'unknown error'}",
                cb,
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
                cb,
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
            cb,
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
            callbacks=_CICycleCallbacks.from_execute(cb).build(),
        )
        exec_result["exhausted"] = cycle_result.exhausted
        return exec_result

    async def _dispatch_execute(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_number: int,
        poll_interval_seconds: float,
        cb: ExecutePhaseCallbacks,
    ) -> AgentJobResult:
        """Dispatch the execute agent job (or use injected callback)."""
        if cb.dispatch_execute is not None:
            return await cb.dispatch_execute(
                project_id, spec, issue_number, poll_interval_seconds
            )
        result = await workflow.execute_activity(
            "dispatch_agent_job",
            DispatchInput(
                project_id,
                issue_number,
                spec,
                poll_interval_seconds=poll_interval_seconds,
            ),
            result_type=AgentJobResult,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
            task_queue=JOB_DISPATCH_QUEUE,
        )
        if result.status != JobStatus.AWAITING_HUMAN.value:
            await self._cleanup(result.job_name, cb)
        return result

    async def _answer_questions(
        self,
        project_id: str,
        issue_no: int,
        result: AgentJobResult,
        cb: ExecutePhaseCallbacks,
    ) -> AgentJobResult:
        """Resolve mid-run AWAITING_HUMAN questions.

        Only dispatches a question resolver when the job is paused
        (AWAITING_HUMAN).  Otherwise the original result flows through
        unchanged — this preserves the zero-commits / parked-path where
        the status is COMPLETE or FAILED but no human question was asked.
        """
        if result.status != JobStatus.AWAITING_HUMAN.value:
            return result
        if cb.answer_question is not None:
            return await cb.answer_question(project_id, issue_no, result)
        # Default: no question resolution — return result as-is.
        return result

    async def _comment(
        self, project_id: str, issue_number: int, body: str, cb: ExecutePhaseCallbacks
    ) -> None:
        """Post a GitHub Issue/PR comment."""
        if cb.post_comment is not None:
            await cb.post_comment(project_id, issue_number, body)
            return
        await workflow.execute_activity(
            "post_github_comment",
            GithubNotificationInput(
                issue_number=issue_number,
                project_id=project_id,
                body=body,
            ),
            start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _cleanup(self, job_name: str, cb: ExecutePhaseCallbacks) -> None:
        """Delete the output ConfigMap for a completed job."""
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


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# CICycle bridge (ExecutePhase delegates CI fix to CICycle)
# --------------------------------------------------------------------------- #


class _CICycleCallbacks:
    """Bridge from ExecutePhase callbacks to CICycle's callback model.

    CICycle needs: ``poll_ci``, ``dispatch_fix``, ``post_comment``,
    ``kpi_bump``, ``cleanup``.  ExecutePhase only exposes
    ``dispatch_execute``, ``answer_question``, ``post_comment``, ``kpi_bump``.
    This bridge maps what it can and lets CICycle fall back to its own
    ``default()`` for the rest.
    """

    def __init__(
        self,
        execute_cb: Optional[ExecutePhaseCallbacks] = None,
    ) -> None:
        self._execute_cb = execute_cb or ExecutePhaseCallbacks.default()

    @classmethod
    def from_execute(
        cls, execute_cb: Optional[ExecutePhaseCallbacks] = None
    ) -> "_CICycleCallbacks":
        """Create from ExecutePhase's ExecutePhaseCallbacks instance."""
        return cls(execute_cb)

    def build(self) -> Optional[Any]:
        """Build a CICycle callbacks object, or ``None`` to use defaults.

        Returns ``None`` when *all* required callbacks are ``None``,
        signalling that CICycle should fall back to its own activity
        defaults.
        """
        needs = {
            "post_comment": self._execute_cb.post_comment,
            "kpi_bump": self._execute_cb.kpi_bump,
        }
        if all(v is None for v in needs.values()):
            return None

        cic_cb = __import__("devloop.phases.cycle", fromlist=["_Callbacks"])._Callbacks
        return cic_cb(
            post_comment=self._execute_cb.post_comment,
            kpi_bump=self._execute_cb.kpi_bump,
        )
