"""ImprovePhase — dispatches a sentrux quality-improvement agent job.

Wraps the queued-comment posting, improve-phase dispatch, and result
deserialization from ``CodeQualityWorkflow`` as a standalone deep module
with a small interface: ``run(inp, callbacks)``.

The phase returns a dict with keys ``summary``, ``parent_issue_number`` so
the caller (the workflow) can post the final completion comment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Optional

from temporalio.common import RetryPolicy

if TYPE_CHECKING:
    from ..execution import AgentJobResult
    from ..shared import TaskSpec

# Type aliases for injectable callbacks.
_DispatchCallback = Callable[  # noqa: UP006
    [str, "TaskSpec", int, float], Coroutine[Any, Any, "AgentJobResult"]
]
_PostCommentCallback = Callable[  # noqa: UP006
    [str, int, str], Coroutine[Any, Any, None]
]


@dataclass
class _Callbacks:
    """Callback set for ImprovePhase.run().

    When all fields are ``None``, default Temporal activity paths are used.
    """

    dispatch: Optional[_DispatchCallback] = None
    post_comment: Optional[_PostCommentCallback] = None

    @classmethod
    def default(cls) -> "_Callbacks":
        """Return a callbacks instance that delegates to Temporal activities."""
        return cls()


class ImprovePhase:
    """Dispatch a sentrux quality-improvement agent job.

    Stateless — all context flows through ``run`` parameters.
    """

    async def run(
        self,
        project_id: str,
        report: str,
        parent_issue_number: int,
        agent_label: str,
        callbacks: Optional[_Callbacks] = None,
    ) -> dict:
        """Run the improve phase.

        Parameters
        ----------
        project_id: str
            The project identifier.
        report: str
            The sentrux report text from the scan.
        parent_issue_number: int
            The parent issue number.
        agent_label: str
            The agent label to use for the improve dispatch.
        callbacks: _Callbacks, optional
            Injected callbacks for testing.

        Returns
        -------
        dict
            A result dict with keys ``summary``, ``parent_issue_number``.
        """
        cb = callbacks or _Callbacks.default()

        # 1. Post queued comment before improve dispatch.
        await self._comment(
            project_id,
            parent_issue_number,
            "⏳ queued — filing improvement issues",
            cb,
        )

        # 2. Dispatch improve phase.
        from ..shared import TaskSpec

        spec = TaskSpec(
            phase="code_quality_improve",
            project_id=project_id,
            issue_number=parent_issue_number,
            extra={
                "sentrux_report": report,
                "parent_issue_number": parent_issue_number,
                "agent_label": agent_label,
            },
        )
        improve_result = await self._dispatch(
            project_id,
            spec,
            issue_number=parent_issue_number,
            cb=cb,
        )

        # 3. Post completion comment with summary.
        summary = str(improve_result.summary or "")
        await self._comment(
            project_id,
            parent_issue_number,
            f"📋 Filed improvement issues — see sub-issues below.\n\n{summary}",
            cb,
        )

        return {
            "summary": summary,
            "parent_issue_number": parent_issue_number,
        }

    async def _dispatch(
        self,
        project_id: str,
        spec: "TaskSpec",
        issue_number: int,
        cb: _Callbacks,
    ) -> "AgentJobResult":
        """Dispatch the improve agent job (or use injected callback)."""
        from ..execution import AgentJobResult, DispatchInput

        if cb.dispatch is not None:
            return await cb.dispatch(project_id, spec, issue_number, 5.0)

        from temporalio import workflow
        from .._constants import _ACTIVITY_TIMEOUT, JOB_DISPATCH_QUEUE

        result = await workflow.execute_activity(
            "dispatch_agent_job",
            DispatchInput(
                project_id,
                issue_number,
                spec,
                poll_interval_seconds=5.0,
            ),
            result_type=AgentJobResult,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=3),
            task_queue=JOB_DISPATCH_QUEUE,
        )
        return result

    async def _comment(
        self, project_id: str, issue_number: int, body: str, cb: _Callbacks
    ) -> None:
        """Post a GitHub Issue/PR comment (or use injected callback)."""
        from ..github import GithubNotificationInput
        from .._constants import _GITHUB_COMMENT_TIMEOUT
        from temporalio import workflow

        if cb.post_comment is not None:
            await cb.post_comment(project_id, issue_number, body)
        else:
            await workflow.execute_activity(
                "post_github_comment",
                GithubNotificationInput(
                    issue_number=issue_number,
                    project_id=project_id,
                    body=body,
                ),
                start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=3),
            )


# Re-export for convenience.
ImprovePhaseCallbacks = _Callbacks
