"""PRCommentWorkflow — respond to reviewer feedback on open agent PRs (#78).

Triggered by ``webhook.py`` when a human posts a ``pull_request_review`` or an
``@devloop-bot``-mentioning ``issue_comment`` on an open ``agent/issue-<N>``
PR. The workflow re-engages the agent on the existing branch:

    "⏳ queued" ─▶ Phase.PR_COMMENT job (PR number + comment/review body;
                   the agent fetches the diff itself via ``gh pr diff``)
                 ─▶ CI Fix Loop (reuse of ``_WorkflowCommon._ci_fix_loop``)
                 ─▶ request reviewer + post result comment

A concurrent run for the same PR (workflow ID
``pr-comment-{owner}-{repo}-{pr_number}``, ``TERMINATE_EXISTING`` conflict
policy) terminates the in-flight run and starts fresh — the newest comment
wins as context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from temporalio import workflow

from . import dev_loop_logic as logic
from ._constants import _GITHUB_COMMENT_TIMEOUT, _RETRY
from ._workflow_common import _WorkflowCommon
from .shared import (
    GetPRBranchInput,
    JobStatus,
    Phase,
    TaskSpec,
)

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
        same lazy-resolution pattern as ``DevLoopInput.from_env``: called only
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


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@workflow.defn
class PRCommentWorkflow(_WorkflowCommon):
    def __init__(self) -> None:
        pass

    @workflow.run
    async def run(self, inp: PRCommentInput) -> PRCommentResult:
        issue_no = inp.issue_number or inp.pr_number

        await self._comment(
            inp.project_id,
            issue_no,
            "⏳ queued — agent is responding to reviewer feedback",
        )

        # `issue_comment` (an ``@devloop-bot`` mention) webhook payloads carry
        # no `pull_request.head.ref` — only `pull_request_review` does — so
        # `inp.branch` arrives empty. Resolve it from the PR number before
        # dispatching: an empty branch becomes `git clone --branch ''`, which
        # fails the Agent Execution Job outright (issue #101).
        branch = inp.branch
        if not branch:
            branch = await workflow.execute_activity(
                "get_pr_branch",
                GetPRBranchInput(inp.project_id, inp.pr_number),
                start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
                retry_policy=_RETRY,
            )
        if not branch:
            await self._comment(
                inp.project_id,
                issue_no,
                f"❌ Could not respond to feedback — could not resolve PR #{inp.pr_number}'s branch",
            )
            return PRCommentResult(
                status="failed",
                pr_number=inp.pr_number,
                detail="branch resolution failed",
            )

        # `_handle_pull_request_review` already filters to agent-owned PRs
        # before starting this workflow (the head ref comes free in that
        # payload); a comment-sourced run resolves its branch here instead, so
        # it must be re-checked — otherwise an `@devloop-bot` mention on *any*
        # open PR would reach the entrypoint's `force=True` push and clobber a
        # human's branch (issue #101).
        if not _AGENT_BRANCH.match(branch):
            await self._comment(
                inp.project_id,
                issue_no,
                f"❌ Could not respond to feedback — PR #{inp.pr_number} isn't an agent-owned PR (branch `{branch}`)",
            )
            return PRCommentResult(
                status="failed",
                pr_number=inp.pr_number,
                detail="not an agent-owned branch",
            )

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
        )

        if result.status != JobStatus.COMPLETE.value:
            await self._comment(
                inp.project_id,
                issue_no,
                f"❌ Could not respond to feedback — {result.error or 'unknown error'}",
            )
            return PRCommentResult(
                status="failed",
                pr_number=inp.pr_number,
                detail=result.error or "phase failed",
            )

        # Resolve a usable pr_url for the CI fix loop's `pr_number_from_url`
        # parsing — prefer the agent's reported URL; if it's empty or doesn't
        # carry a `/pull/<N>` suffix, synthesize one from the already-known PR
        # number so `_ci_fix_loop` can still poll CI checks for this PR.
        pr_url = result.pr_url
        if not logic.pr_number_from_url(pr_url) and inp.pr_number:
            pr_url = f"/pull/{inp.pr_number}"

        exec_result = {
            "issue_id": issue_no,
            "branch": result.branch or branch,
            "pr_url": pr_url,
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

        await self._request_reviewer(inp.project_id, inp.pr_number)

        note = (
            " ⚠️ CI is still failing after exhausting the CI fix attempts —"
            " please take another look."
            if exhausted
            else ""
        )
        summary = (result.summary or "").strip()
        body = f"👀 Addressed your feedback on PR #{inp.pr_number}.{note}"
        if summary:
            body += f"\n\n{summary}"
        await self._comment(inp.project_id, issue_no, body)

        return PRCommentResult(
            status="completed",
            pr_number=inp.pr_number,
            commits=result.commits,
            exhausted=exhausted,
        )
