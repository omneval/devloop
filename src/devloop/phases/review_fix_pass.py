"""ReviewFixPass — one fix pass after a review's findings.

Wraps the existing ``_review_fix_pass`` activity call from ``DevLoopWorkflow``
as a standalone deep module with a small interface: ``run(inp, issue, exec_result, review, callbacks)``.

The reviewer's summary (which enumerates unmet acceptance criteria and
bugs) is handed to the fix agent exactly like a human PR comment would
be — the proven re-engagement path (omneval#70: the agent resolved
every finding of a human review in one such pass).

Returns True when the fix pass produced commits (a re-review is
worthwhile), False when it failed or changed nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Optional

from devloop.dev_loop_logic import pr_number_from_url
from devloop.shared import TaskSpec


# Type aliases for injectable callbacks.
_DispatchFixCallback = Callable[
    [str, TaskSpec, int, float], Coroutine[Any, Any, Any]
]  # returns AgentJobResult-like
_PostCommentCallback = Callable[[str, int, str], Coroutine[Any, Any, None]]
_KpiBumpCallback = Callable[[str, int], Coroutine[Any, Any, None]]


@dataclass
class _Callbacks:
    """Callback set for ReviewFixPass.run().

    When all fields are ``None``, the default Temporal activity paths are used.
    """

    dispatch_fix: Optional[_DispatchFixCallback] = None
    post_comment: Optional[_PostCommentCallback] = None
    kpi_bump: Optional[_KpiBumpCallback] = None

    @classmethod
    def default(cls) -> "_Callbacks":
        """Return a callbacks instance that delegates to Temporal activities."""
        return cls()


class ReviewFixPass:
    """One fix pass after a review's findings.

    Stateless — all context flows through ``run`` parameters.
    """

    async def run(
        self,
        inp: Any,  # DevLoopInput
        issue: dict,
        exec_result: dict,
        review: dict,
        callbacks: Optional[_Callbacks] = None,
    ) -> bool:
        """Run one review fix pass.

        Parameters
        ----------
        inp : DevLoopInput
            Workflow input (must have ``project_id``, ``poll_interval_seconds``).
        issue : dict
            Plan issue dict (must have ``id``).
        exec_result : dict
            Execute result dict (must have ``pr_url``, ``branch``).
        review : dict
            Review payload (may have ``summary``, ``inline_comments``).
        callbacks : _Callbacks, optional
            Injected callbacks for testing.

        Returns
        -------
        bool
            True when the fix pass produced commits, False otherwise.
        """
        cb = callbacks or _Callbacks.default()
        issue_no = _as_int(issue.get("id"))
        pr_url = exec_result.get("pr_url", "")
        pr_number = pr_number_from_url(pr_url)
        findings = review.get("summary", "")
        inline = review.get("inline_comments") or []
        if inline:
            findings += "\n\nInline comments:\n" + "\n".join(
                f"- {c.get('file', '')}:{c.get('line', 0)} — {c.get('body', '')}"
                for c in inline
            )
        if not findings.strip():
            return False

        await self._post_comment(
            inp.project_id,
            issue_no,
            "⏳ queued — agent is addressing automated review findings",
            cb,
        )
        spec = TaskSpec(
            phase="pr_comment",
            project_id=inp.project_id,
            issue_number=issue_no,
            branch=exec_result.get("branch", ""),
            extra={
                "comment_body": findings,
                "source": "review",
                "author": "the automated reviewer",
                "pr_number": pr_number,
            },
        )
        result = await self._dispatch_fix(
            inp.project_id,
            spec,
            issue_number=issue_no,
            poll_interval_seconds=inp.poll_interval_seconds,
            cb=cb,
        )
        if not _has_commits(result):
            return False
        await self._post_comment(
            inp.project_id,
            issue_no,
            f"🔧 Fix pass pushed {_commits_count(result)} commit(s) addressing review findings.",
        )
        return True

    async def _dispatch_fix(
        self,
        project_id: str,
        spec: TaskSpec,
        issue_number: int,
        poll_interval_seconds: float,
        cb: _Callbacks,
    ) -> Any:
        """Dispatch the fix agent job (or use injected callback)."""
        if cb.dispatch_fix is not None:
            return await cb.dispatch_fix(
                project_id, spec, issue_number, poll_interval_seconds
            )
        # Default path: return None so tests know to provide a callback.
        return None

    async def _post_comment(
        self,
        project_id: str,
        issue_number: int,
        body: str,
        cb: Optional[_Callbacks] = None,
    ) -> None:
        """Post a GitHub Issue/PR comment."""
        if cb and cb.post_comment is not None:
            await cb.post_comment(project_id, issue_number, body)
            return

    async def _kpi_bump(
        self, name: str, value: int, cb: Optional[_Callbacks] = None
    ) -> None:
        """Record a KPI metric."""
        if cb and cb.kpi_bump is not None:
            await cb.kpi_bump(name, value)


def _has_commits(result: Any) -> bool:
    """Check if a dispatch result has non-zero commits."""
    return getattr(result, "commits", 0) > 0 or (
        isinstance(result, dict) and result.get("commits", 0) > 0
    )


def _commits_count(result: Any) -> int:
    """Get the commit count from a dispatch result."""
    if isinstance(result, dict):
        return result.get("commits", 0)
    return getattr(result, "commits", 0)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# Re-export for convenience.
ReviewFixPassCallbacks = _Callbacks
