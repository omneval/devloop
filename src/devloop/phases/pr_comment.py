"""PRCommentPhase — respond to reviewer feedback on open agent PRs (#78).

Wraps the branch resolution, validation, and agent dispatch from
``PRCommentWorkflow`` as a standalone deep module with a small
interface: ``run(inp, callbacks)``.

After the phase, the caller (typically ``PRCommentWorkflow``) runs the
CI fix cycle and requests a reviewer — those are separate phases.

The ``_AGENT_BRANCH`` regex guards against clobbering human branches
(issue #101).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

from .._constants import _ACTIVITY_TIMEOUT, _GITHUB_COMMENT_TIMEOUT, _RETRY
from ..dev_loop_logic import pr_number_from_url

if TYPE_CHECKING:
    from ..shared import AgentJobResult, TaskSpec

# Agent issue branches are named ``agent/issue-<N>[-slug]`` (see entrypoint.py /
# github_ops._AGENT_BRANCH / webhook._AGENT_BRANCH). ``_handle_pull_request_review``
# already filters on this before starting the workflow (the head ref comes free
# in that payload); ``_handle_issue_comment`` can't — an ``issue_comment``
# payload carries no head ref, only a PR number — so it dispatches with an
# empty ``branch`` and lets ``get_pr_branch`` resolve it here. Re-checking the
# resolved branch closes that gap: without it, an ``@devloop-bot`` mention on
# *any* open PR (not just agent-owned ones) would resolve a real branch and
# proceed to the entrypoint's ``force=True`` push — clobbering a human's work
# (issue #101).
_AGENT_BRANCH = re.compile(r"^agent/issue-(\d+)")


# Type aliases for injectable callbacks.
_GetBranchCallback = Callable[[str, int], Coroutine[Any, Any, str]]
_DispatchCallback = Callable[
    [str, "TaskSpec", int, float], Coroutine[Any, Any, "AgentJobResult"]
]
_PostCommentCallback = Callable[[str, int, str], Coroutine[Any, Any, None]]


@dataclass
class _Callbacks:
    """Callback set for PRCommentPhase.run().

    When all fields are ``None``, the default Temporal activity paths are used.
    """

    post_comment: Optional[_PostCommentCallback] = None
    get_branch: Optional[_GetBranchCallback] = None
    dispatch: Optional[_DispatchCallback] = None

    @classmethod
    def default(cls) -> "_Callbacks":
        """Return a callbacks instance that delegates to Temporal activities."""
        return cls()


@dataclass
class PRCommentPhaseResult:
    """Result of a PRCommentPhase run.

    Attributes
    ----------
    exec_result : dict | None
        The agent job result dict, or ``None`` when an error prevented
        dispatch.  When present, carries ``issue_id``, ``branch``,
        ``pr_url``, and ``commits`` keys.
    error : str | None
        A human-readable error description, or ``None`` on success.
    """

    exec_result: dict | None = None
    error: str | None = None


class PRCommentPhase:
    """Resolve a PR's branch, validate it's agent-owned, and dispatch the
    PR_COMMENT agent job.

    Stateless — all context flows through ``run`` parameters.
    """

    async def run(
        self,
        inp: Any,  # PRCommentInput
        callbacks: Optional[_Callbacks] = None,
    ) -> PRCommentPhaseResult:
        """Run the PR comment phase.

        Parameters
        ----------
        inp : PRCommentInput
            Workflow input (must have ``project_id``, ``pr_number``,
            ``issue_number``, ``branch``, ``comment_body``, ``source``,
            ``author``, ``poll_interval_seconds``).
        callbacks : _Callbacks, optional
            Injected callbacks for testing.

        Returns
        -------
        PRCommentPhaseResult
            The exec result dict (with issue_id, branch, pr_url, commits),
            or an error if the phase failed.
        """
        from ..shared import JobStatus, Phase, TaskSpec

        cb = callbacks or _Callbacks.default()
        issue_no = inp.issue_number or inp.pr_number

        # 1. Queue comment (caller may also post this, but we do it here
        #    for standalone usability).
        await self._comment(
            inp.project_id,
            issue_no,
            "⏳ queued — agent is responding to reviewer feedback",
            cb,
        )

        # 2. Branch resolution — `issue_comment` payloads carry no head ref,
        #    so `inp.branch` arrives empty. Resolve it via the callback.
        branch = inp.branch
        if not branch:
            branch = await self._get_branch(inp.project_id, inp.pr_number, cb)
        if not branch:
            await self._comment(
                inp.project_id,
                issue_no,
                f"❌ Could not respond to feedback — could not resolve PR #{inp.pr_number}'s branch",
                cb,
            )
            return PRCommentPhaseResult(
                exec_result=None,
                error="branch resolution failed",
            )

        # 3. Branch validation — must match agent/issue-<N>.
        if not _AGENT_BRANCH.match(branch):
            await self._comment(
                inp.project_id,
                issue_no,
                f"❌ Could not respond to feedback — PR #{inp.pr_number} isn't an agent-owned PR (branch `{branch}`)",
                cb,
            )
            return PRCommentPhaseResult(
                exec_result=None,
                error="not an agent-owned branch",
            )

        # 4. TaskSpec construction and dispatch.
        spec = TaskSpec(
            phase=Phase.PR_COMMENT.value,
            project_id=inp.project_id,
            issue_number=issue_no,
            branch=branch,
            extra={
                "pr_number": inp.pr_number,
                "comment_body": inp.comment_body,
                "source": inp.source,
                "author": inp.author,
            },
        )
        result = await self._dispatch(
            inp.project_id,
            spec,
            issue_number=issue_no,
            poll_interval_seconds=inp.poll_interval_seconds,
            cb=cb,
        )

        if result.status != JobStatus.COMPLETE.value:
            await self._comment(
                inp.project_id,
                issue_no,
                f"❌ Could not respond to feedback — {result.error or 'unknown error'}",
                cb,
            )
            return PRCommentPhaseResult(
                exec_result=None,
                error=result.error or "phase failed",
            )

        # 5. Return the exec result dict for the caller (CI fix cycle, etc).
        pr_url = result.pr_url or ""
        if not pr_number_from_url(pr_url) and inp.pr_number:
            pr_url = f"/pull/{inp.pr_number}"

        return PRCommentPhaseResult(
            exec_result={
                "issue_id": issue_no,
                "branch": result.branch or branch,
                "pr_url": pr_url,
                "commits": result.commits,
                "summary": result.summary or "",
            },
            error=None,
        )

    async def _get_branch(self, project_id: str, pr_number: int, cb: _Callbacks) -> str:
        """Resolve a PR's branch via callback or default activity."""
        from ..shared import GetPRBranchInput

        if cb.get_branch is not None:
            return await cb.get_branch(project_id, pr_number)
        result = await workflow.execute_activity(
            "get_pr_branch",
            GetPRBranchInput(project_id, pr_number),
            result_type=str,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_RETRY,
        )
        return result or ""

    async def _dispatch(
        self,
        project_id: str,
        spec: "TaskSpec",
        issue_number: int,
        poll_interval_seconds: float,
        cb: _Callbacks,
    ) -> "AgentJobResult":
        """Dispatch the PR comment agent job (or use injected callback)."""
        from ..shared import (
            JOB_DISPATCH_QUEUE,
            AgentJobResult,
            DispatchInput,
        )

        if cb.dispatch is not None:
            return await cb.dispatch(
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
            retry_policy=RetryPolicy(maximum_attempts=3),
            task_queue=JOB_DISPATCH_QUEUE,
        )
        return result

    async def _comment(
        self, project_id: str, issue_number: int, body: str, cb: _Callbacks
    ) -> None:
        """Post a GitHub Issue/PR comment."""
        from ..shared import GithubNotificationInput

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
                retry_policy=_RETRY,
            )
