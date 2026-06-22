"""Phase.CI_FIX retry loop — reusable CI fix cycle (#76).

Runs the CI fix loop: poll CI checks, dispatch fix jobs when red,
re-poll until green or exhausted.  Shared between DevLoopWorkflow and
PRCommentWorkflow so both workflows don't duplicate this logic.

The loop respects bounded backoff for pending CI runs (issue #90) so that
slow-but-healthy checks don't burn limited fix attempts.

All I/O operations delegate through the ``CICycleOps`` sub-protocol;
the module has no private I/O methods.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Optional

from temporalio import workflow

from ..cichecks import CIChecksResult
from ..phases.phase_ops import PhaseOps

if TYPE_CHECKING:
    from ..execution import TaskSpec
    from .ci_cycle_ops import CICycleOps

# Bounded backoff for "CI still pending" re-polls within a single ci_fix
# attempt slot — caps how long CICycle waits on a CI run that never
# resolves before it gives up rather than looping forever (issue #90).
_CI_PENDING_POLL_LIMIT = 12


@dataclass
class CICycleResult:
    """Result of a CI fix cycle."""

    exhausted: bool
    commits: int


class _Callbacks(PhaseOps):
    """Backward-compatible shim that extends the unified ``PhaseOps`` protocol.

    This class exists only for callers that still construct
    ``_Callbacks(poll_ci=..., dispatch_fix=..., ...)`` directly.  It
    inherits from ``PhaseOps`` so all downstream code uses the unified
    protocol seamlessly.
    """

    def __init__(
        self,
        poll_ci: Optional[
            Callable[[str, int], Coroutine[Any, Any, CIChecksResult]]
        ] = None,
        dispatch_fix: Optional[
            Callable[[str, "TaskSpec", int, float], Coroutine[Any, Any, int]]
        ] = None,
        post_comment: Optional[
            Callable[[str, int, str], Coroutine[None, None, None]]
        ] = None,
        kpi_bump: Optional[Callable[[str, int], Coroutine[None, None, None]]] = None,
        cleanup: Optional[Callable[[str], Coroutine[None, None, None]]] = None,
    ) -> None:
        super().__init__(
            poll_ci=poll_ci,
            dispatch_fix=dispatch_fix,
            post_comment=post_comment,
            kpi_bump=kpi_bump,
            cleanup=cleanup,
        )


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
        callbacks: Optional[PhaseOps] = None,
    ) -> CICycleResult:
        """Run the CI fix loop.

        Polls CI, dispatches fix jobs when red, re-polls until green or
        every fix attempt is spent.

        All I/O operations delegate through the *ci_ops* sub-protocol on
        the *callbacks* parameter (a ``PhaseOps`` instance).  When a
        callback field on ``ci_ops`` is ``None`` the corresponding
        fallback accesses the matching field on ``callbacks`` directly.

        Parameters
        ----------
        callbacks : PhaseOps, optional
            Injected callbacks for testing.  When a callback field is
            ``None``, the corresponding ``PhaseOps`` method falls back
            to its Temporal activity path.
            ``None`` is accepted — CICycle short-circuits early when there
            is no PR URL, so this is never required for that path.

        Returns
        -------
        CICycleResult
            ``exhausted=True`` when every fix attempt is spent without CI
            going green.
        """
        ops = PhaseOps()
        pr_number = ops.pr_number_from_url(exec_result.get("pr_url", ""))
        if pr_number <= 0:
            return CICycleResult(exhausted=False, commits=0)

        cb = callbacks or PhaseOps()
        ci_ops: CICycleOps = cb.ci_ops

        max_iters = ci_fix_max_iterations
        attempt = 0
        pending_polls = 0
        total_commits = 0

        while attempt < max_iters:
            checks = await self._poll_ci(project_id, pr_number, cb, ci_ops)
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

            await self._kpi_bump(ci_ops, cb)

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
                "phase": "ci_fix",
                "project_id": project_id,
                "issue_number": issue_no,
                "branch": exec_result.get("branch", ""),
                "extra": {"ci_check_failures": failures},
            }

            await self._comment(
                project_id,
                pr_number,
                f"⏳ queued — CI fix attempt {attempt}/{max_iters}",
                cb,
                ci_ops,
            )

            commits = await self._dispatch_fix(
                project_id,
                ci_ops,
                attempt,
                max_iters,
                spec_dict,
                issue_no,
                poll_interval_seconds,
                cb,
            )
            total_commits += commits

            emoji = "🔧" if commits > 0 else "❌"
            msg = (
                f"{emoji} CI fix attempt {attempt}/{max_iters} — "
                f"{'pushed ' + str(commits) + ' commit(s)' if commits > 0 else 'failed'}"
            )
            await self._comment(project_id, pr_number, msg, cb, ci_ops)

        # Re-check before declaring exhaustion.
        final_checks = await self._poll_ci(project_id, pr_number, cb, ci_ops)
        return CICycleResult(
            exhausted=not final_checks.all_passed,
            commits=total_commits,
        )

    # ------------------------------------------------------------------
    # Internal helpers — each prefers ci_ops, falls back to callbacks
    # ------------------------------------------------------------------

    async def _poll_ci(
        self,
        project_id: str,
        pr_number: int,
        callbacks: PhaseOps,
        ci_ops: CICycleOps,
    ) -> CIChecksResult:
        """Poll CI checks, preferring ci_ops.poll_ci."""
        ops = PhaseOps()
        callback = ci_ops.poll_ci or callbacks.poll_ci
        return await ops.poll(project_id, pr_number, callback=callback)

    async def _kpi_bump(
        self,
        ci_ops: CICycleOps,
        callbacks: PhaseOps,
    ) -> None:
        """Increment the ci_fix_iterations KPI, preferring ci_ops."""
        if ci_ops.kpi_bump is not None:
            await ci_ops.kpi_bump("ci_fix_iterations", 1)
        elif callbacks.kpi_bump is not None:
            await callbacks.kpi_bump("ci_fix_iterations", 1)

    async def _dispatch_fix(
        self,
        project_id: str,
        ci_ops: CICycleOps,
        attempt: int,
        max_iters: int,
        spec_dict: dict[str, Any],
        issue_no: int,
        poll_interval_seconds: float,
        callbacks: PhaseOps,
    ) -> int:
        """Dispatch a CI fix job, preferring ci_ops.dispatch_fix."""
        # Deferred import to avoid circular dependencies.
        from ..execution import TaskSpec

        spec = TaskSpec(**spec_dict)
        if ci_ops.dispatch_fix is not None:
            return await ci_ops.dispatch_fix(
                project_id, spec, issue_no, poll_interval_seconds
            )
        if callbacks.dispatch_fix is not None:
            return await callbacks.dispatch_fix(
                project_id, spec, issue_no, poll_interval_seconds
            )
        # No fix callback configured — treat as failure.
        return 0

    async def _comment(
        self,
        project_id: str,
        pr_number: int,
        body: str,
        callbacks: PhaseOps,
        ci_ops: CICycleOps,
    ) -> None:
        """Post a comment via ci_ops.comment or callbacks.post_comment."""
        ops = PhaseOps()
        await ops._phase_comment(
            project_id,
            pr_number,
            body,
            callback=ci_ops.comment or callbacks.post_comment,
        )


# Re-export for convenience.
CICycleCallbacks = _Callbacks
