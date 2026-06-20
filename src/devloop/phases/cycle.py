"""Phase.CI_FIX retry loop — reusable CI fix cycle (#76).

Runs the CI fix loop: poll CI checks, dispatch fix jobs when red,
re-poll until green or exhausted.  Shared between DevLoopWorkflow and
PRCommentWorkflow so both workflows don't duplicate this logic.

The loop respects bounded backoff for pending CI runs (issue #90) so that
slow-but-healthy checks don't burn limited fix attempts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Coroutine, Optional

from temporalio import workflow

from ..phases.phase_ops import PhaseOps
from ..shared import (
    CIChecksResult,
    JOB_DISPATCH_QUEUE,
    Phase,
    TaskSpec,
)

# Bounded backoff for "CI still pending" re-polls within a single ci_fix
# attempt slot — caps how long CICycle waits on a CI run that never
# resolves before it gives up rather than looping forever (issue #90).
_CI_PENDING_POLL_LIMIT = 12


@dataclass
class CICycleResult:
    """Result of a CI fix cycle."""

    exhausted: bool
    commits: int


# Type aliases for injectable callbacks.
_PollCiCallback = Callable[[str, int], Coroutine[Any, Any, CIChecksResult]]
_DispatchFixCallback = Callable[
    [str, int, dict, float], Coroutine[Any, Any, int]
]  # returns commits count
_PostCommentCallback = Callable[[str, int, str], Coroutine[None, None, None]]
_KpiBumpCallback = Callable[[str, int], Coroutine[None, None, None]]
_CleanupCallback = Callable[[str], Coroutine[None, None, None]]


@dataclass
class _Callbacks:
    """Callback set for CICycle.run().

    When all fields are ``None``, the default Temporal activity paths are used.
    """

    poll_ci: Optional[_PollCiCallback] = None
    dispatch_fix: Optional[_DispatchFixCallback] = None
    post_comment: Optional[_PostCommentCallback] = None
    kpi_bump: Optional[_KpiBumpCallback] = None
    cleanup: Optional[_CleanupCallback] = None

    @classmethod
    def default(cls) -> "_Callbacks":
        """Return a callbacks instance that delegates to Temporal activities."""
        return cls()


class CICycle:
    """Reusable CI fix cycle.

    Each instance is stateless; the caller passes all context (project_id,
    issue_no, exec_result) per invocation.  This keeps the module deep —
    the interface is a single ``run`` method.
    """

    async def run(
        self,
        *,
        project_id: str,
        issue_no: int,
        exec_result: dict,
        ci_fix_max_iterations: int,
        poll_interval_seconds: float = 5.0,
        callbacks: Optional[_Callbacks] = None,
    ) -> CICycleResult:
        """Run the CI fix loop.

        Polls CI, dispatches fix jobs when red, re-polls until green or
        every fix attempt is spent.

        Parameters
        ----------
        callbacks : _Callbacks, optional
            Injected callbacks for testing.  When omitted, the default
            activity path is used.

        Returns
        -------
        CICycleResult
            ``exhausted=True`` when every fix attempt is spent without CI
            going green.
        """
        cb = callbacks or _Callbacks.default()
        ops = PhaseOps()
        pr_number = ops.pr_number_from_url(exec_result.get("pr_url", ""))
        if pr_number <= 0:
            return CICycleResult(exhausted=False, commits=0)

        max_iters = ci_fix_max_iterations
        attempt = 0
        pending_polls = 0
        total_commits = 0

        while attempt < max_iters:
            checks = await ops.poll(project_id, pr_number, callback=cb.poll_ci)
            if checks.all_passed:
                return CICycleResult(exhausted=False, commits=total_commits)

            if checks.pending and not checks.failures:
                if pending_polls >= _CI_PENDING_POLL_LIMIT:
                    return CICycleResult(exhausted=True, commits=total_commits)
                pending_polls += 1
                await workflow.sleep(
                    timedelta(seconds=poll_interval_seconds * pending_polls)
                )
                continue

            pending_polls = 0
            attempt += 1

            if cb.kpi_bump:
                await cb.kpi_bump("ci_fix_iterations", 1)

            failures = [
                {
                    "name": f.name,
                    "conclusion": f.conclusion,
                    "details_url": f.details_url,
                    "summary": f.summary,
                }
                for f in (checks.failures or [])
            ]
            spec_dict: dict[str, Any] = {
                "phase": Phase.CI_FIX.value,
                "project_id": project_id,
                "issue_number": issue_no,
                "branch": exec_result.get("branch", ""),
                "extra": {"ci_check_failures": failures},
            }

            await ops.comment(
                project_id,
                pr_number,
                f"⏳ queued — CI fix attempt {attempt}/{max_iters}",
                callback=cb.post_comment,
            )

            if cb.dispatch_fix is not None:
                commits = await cb.dispatch_fix(
                    project_id, issue_no, spec_dict, poll_interval_seconds
                )
            else:
                _result = await ops.dispatch_helper(
                    project_id,
                    TaskSpec(**spec_dict),
                    issue_no,
                    poll_interval_seconds,
                    dispatch_callback=None,
                    task_queue=JOB_DISPATCH_QUEUE,
                )
                await ops.cleanup(_result.job_name, callback=cb.cleanup)
                commits = _result.commits
            total_commits += commits

            if commits > 0:
                await ops.comment(
                    project_id,
                    pr_number,
                    f"🔧 CI fix attempt {attempt}/{max_iters} — "
                    f"pushed {commits} commit(s)",
                    callback=cb.post_comment,
                )
            else:
                await ops.comment(
                    project_id,
                    pr_number,
                    f"❌ CI fix attempt {attempt}/{max_iters} failed",
                    callback=cb.post_comment,
                )

        # Re-check before declaring exhaustion.
        final_checks = await ops.poll(project_id, pr_number, callback=cb.poll_ci)
        return CICycleResult(
            exhausted=not final_checks.all_passed,
            commits=total_commits,
        )
