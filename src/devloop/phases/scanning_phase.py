"""ScanningPhase — dispatches a sentrux scan and deserialises the result.

Wraps the parent-issue creation, queued-comment posting, scan dispatch, and
result deserialization from ``CodeQualityWorkflow`` as a standalone deep
module with a small interface: ``run(inp, callbacks)``.

The phase returns a dict with keys ``score``, ``report``, ``scan_error``, and
``error_message`` so the caller (the workflow) can perform the quality-gate
decision and the fail/improve path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Optional

from temporalio.common import RetryPolicy

if TYPE_CHECKING:
    from ..shared import (
        AgentJobResult,
        CreateGithubIssueInput,
        TaskSpec,
    )

# Type aliases for injectable callbacks.
_CreateIssueCallback = Callable[  # noqa: UP006
    ["CreateGithubIssueInput"], Coroutine[Any, Any, int]
]
_DispatchCallback = Callable[  # noqa: UP006
    [str, "TaskSpec", int, float], Coroutine[Any, Any, "AgentJobResult"]
]
_PostCommentCallback = Callable[  # noqa: UP006
    [str, int, str], Coroutine[Any, Any, None]
]


@dataclass
class _Callbacks:
    """Callback set for ScanningPhase.run().

    When all fields are ``None``, default Temporal activity paths are used.
    """

    create_issue: Optional[_CreateIssueCallback] = None
    dispatch: Optional[_DispatchCallback] = None
    post_comment: Optional[_PostCommentCallback] = None

    @classmethod
    def default(cls) -> "_Callbacks":
        """Return a callbacks instance that delegates to Temporal activities."""
        return cls()


class ScanningPhase:
    """Dispatch a sentrux scan and return the deserialized result dict.

    Stateless — all context flows through ``run`` parameters.
    """

    async def run(
        self,
        project_id: str,
        threshold: int,
        callbacks: Optional[_Callbacks] = None,
    ) -> dict:
        """Run the scanning phase.

        Parameters
        ----------
        project_id: str
            The project identifier.
        threshold: int
            The quality threshold (0–10000).
        callbacks: _Callbacks, optional
            Injected callbacks for testing.

        Returns
        -------
        dict
            A result dict with keys ``score``, ``report``, ``scan_error``,
            ``error_message``, and ``parent_issue_number``.
        """
        cb = callbacks or _Callbacks.default()

        # 1. Open parent issue.
        date_str = datetime.now().strftime("%Y-%m-%d")
        parent_issue_number = await self._create_issue(project_id, date_str, cb)

        # 2. Post queued comment before scan.
        await self._comment(
            project_id,
            parent_issue_number,
            "queued — sentrux scan starting",
            cb,
        )

        # 3. Dispatch scan phase.
        from ..shared import TaskSpec

        spec = TaskSpec(
            phase="code_quality_scan",
            project_id=project_id,
            issue_number=parent_issue_number,
            extra={"threshold": threshold},
        )
        scan_result = await self._dispatch(
            project_id,
            spec,
            issue_number=parent_issue_number,
            cb=cb,
        )

        # 4. Deserialize result from plan field.
        plan = scan_result.plan or {}
        result: dict[str, Any] = {
            "score": int(plan.get("score", 0)),
            "report": str(plan.get("report", scan_result.summary)),
            "scan_error": bool(plan.get("scan_error", False)),
            "error_message": str(plan.get("error_message", "")),
            "parent_issue_number": parent_issue_number,
        }
        return result

    async def _create_issue(
        self, project_id: str, date_str: str, cb: _Callbacks
    ) -> int:
        """Create the parent GitHub issue (or use injected callback)."""
        from .._constants import _GITHUB_COMMENT_TIMEOUT
        from ..shared import CreateGithubIssueInput

        title = f"[devloop] Code quality report — {project_id} — {date_str}"
        inp = CreateGithubIssueInput(
            project_id=project_id,
            title=title,
            body="Sentrux scan in progress…",
            labels=["devloop-code-quality"],
        )
        if cb.create_issue is not None:
            return await cb.create_issue(inp)

        from temporalio import workflow

        return await workflow.execute_activity(
            "create_github_issue",
            inp,
            result_type=int,
            start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

    async def _dispatch(
        self,
        project_id: str,
        spec: "TaskSpec",
        issue_number: int,
        cb: _Callbacks,
    ) -> "AgentJobResult":
        """Dispatch the scan agent job (or use injected callback)."""
        from .._constants import _ACTIVITY_TIMEOUT, JOB_DISPATCH_QUEUE
        from ..shared import AgentJobResult, DispatchInput

        if cb.dispatch is not None:
            return await cb.dispatch(project_id, spec, issue_number, 5.0)

        from temporalio import workflow

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
        from .._constants import _GITHUB_COMMENT_TIMEOUT
        from ..github import GithubNotificationInput

        if cb.post_comment is not None:
            await cb.post_comment(project_id, issue_number, body)
            return

        from temporalio import workflow

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
ScanningPhaseCallbacks = _Callbacks
