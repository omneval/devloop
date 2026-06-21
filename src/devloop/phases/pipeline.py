"""PhasePipeline — the Dev Loop orchestration loop.

Controls phase ordering, CI cycle, and notification for a single
project.  It is a plain async class (not a Temporal workflow); the
workflow delegates to it and injects phase callables.

Interface (``run`` method):

    await pipeline.run(
        inp,
        plan_phase=plan_callable,
        execute_phase=execute_callable,
        review_phase=review_callable,
        fix_pass=fix_pass_callable,
        notifier=notify_callable,
    )

Each callable is a plain ``async def`` — the workflow wires its own
methods to them, tests inject mock callables.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from ..shared import WorkflowKpiInput

if TYPE_CHECKING:
    from ..dev_loop import DevLoopInput


def _devloop_result(*args, **kwargs):
    """Lazy-import DevLoopResult to avoid circular imports."""
    from ..dev_loop import DevLoopResult as _DLR

    return _DLR(*args, **kwargs)


# Type for KPI emission: accepts a WorkflowKpiInput and emits the KPIs.
_KpiEmitterFn = Callable[[WorkflowKpiInput], Awaitable[None]]


async def _await_if_needed(coro_or_value):
    """Await if the value is a coroutine, otherwise return it directly."""
    if inspect.isawaitable(coro_or_value):
        return await coro_or_value
    return coro_or_value


async def _ensure_async(fn, *args):
    """Call fn(*args) and await the result if it's a coroutine."""
    result = fn(*args)
    return await _await_if_needed(result)


async def _run_fn(fn, *args):
    """Run an async or sync callable, always awaiting."""
    if inspect.iscoroutinefunction(fn):
        return await fn(*args)
    # If fn is not async, wrap it: make a coroutine function that returns fn(*args)
    result = fn(*args)
    if inspect.isawaitable(result):
        return await result
    return result


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# Type aliases for injected callables.
# Each mirrors the corresponding _WorkflowCommon method signature.
_PlanPhaseFn = Callable[["DevLoopInput", int], Awaitable[dict | None]]
_ExecutePhaseFn = Callable[["DevLoopInput", dict], Awaitable[dict]]
_ReviewPhaseFn = Callable[["DevLoopInput", dict, dict], Awaitable[dict | None]]
_FixPassFn = Callable[["DevLoopInput", dict, dict, dict], Awaitable[bool]]
_NotifyFn = Callable[["DevLoopInput", dict, dict], Awaitable[None]]
_NextIssueFn = Callable[[], Any]  # () -> int, 0/None when nothing queued


# Type for post-round callbacks. The pipeline invokes these after each
# successful round (plan → execute → review → fix → notify) with context
# that the caller (typically a Temporal workflow) uses to emit KPIs.
_PostRoundFn = Callable[[dict, dict, int, str], Awaitable[None]]
# (issue, exec_result, fix_passes, verdict) -> None


class PhasePipeline:
    """Dev Loop orchestration loop.

    Driven by the ``run`` method which accepts five async callables —
    one per phase plus a review fix-pass callable.  The pipeline
    controls round iteration, issue selection, and the plan→execute→
    review→fix→notify ordering.

    All state lives in the callables and the input/result objects;
    this class itself holds nothing.
    """

    async def run(
        self,
        inp: Any,  # DevLoopInput — deferred import to avoid circular dependency
        *,
        plan_phase: _PlanPhaseFn,
        execute_phase: _ExecutePhaseFn,
        review_phase: _ReviewPhaseFn,
        fix_pass: _FixPassFn,
        notifier: _NotifyFn,
        post_round: Optional[_PostRoundFn] = None,
        next_issue: Optional[_NextIssueFn] = None,
    ) -> Any:  # DevLoopResult
        """Run the Dev Loop orchestration.

        Parameters
        ----------
        inp : DevLoopInput
            Workflow input (project, iterations, etc.).
        plan_phase : _PlanPhaseFn
            ``async def plan_phase(inp, rnd) -> dict | None`` —
            returns a plan dict with an ``issues`` list, or ``None``.
        execute_phase : _ExecutePhaseFn
            ``async def execute_phase(inp, issue) -> dict`` —
            returns an exec_result dict (must have ``commits`` key).
        review_phase : _ReviewPhaseFn
            ``async def review_phase(inp, issue, exec_result) -> dict | None`` —
            returns a review dict with a ``verdict`` key.
        fix_pass : _FixPassFn
            ``async def fix_pass(inp, issue, exec_result, review) -> bool`` —
            returns ``True`` when the fix produced commits.
        notifier : _NotifyFn
            ``async def notifier(inp, issue, exec_result) -> None`` —
            posts reviewer notification.
        post_round : _PostRoundFn, optional
            Called after each successful round with ``(issue, exec_result,
            fix_passes, verdict)``.  The caller (typically a Temporal
            workflow) uses this to emit KPIs.
        next_issue : _NextIssueFn, optional
            ``() -> int`` — called once the current ``triggering_issue`` has
            no more issues to plan. A truthy return value is treated as
            another issue to run rounds for (see issue #184: issues labelled
            while a run is already in flight are queued onto the same
            workflow rather than dropped); a falsy value ends the run.

        Returns
        -------
        DevLoopResult
            Final workflow result (completed / failed_plan).
        """
        queued: list[int] = []
        verdicts: dict[int, str] = {}
        # Guard against the same issue repeatedly producing no commits —
        # without this the pipeline could re-plan the same issue for up to
        # max_iterations rounds, each round dispatching agent jobs and posting
        # GitHub comments (issue #204).
        _zero_commit_issue: int = 0
        _zero_commit_count: int = 0

        for rnd in range(1, inp.max_iterations + 1):
            plan = await _run_fn(plan_phase, inp, rnd)
            if plan is None:
                return _devloop_result(
                    "failed_plan",
                    queued_for_review=queued,
                    detail="plan rejected",
                    review_verdicts=verdicts,
                )
            issues = plan.get("issues") or []
            if not issues:
                if next_issue is not None:
                    nxt = await _run_fn(next_issue)
                    if nxt:
                        inp.triggering_issue = nxt
                        continue
                return _devloop_result(
                    "completed",
                    queued_for_review=queued,
                    review_verdicts=verdicts,
                )

            issue = issues[0]  # sequential: one issue per round
            issue_id = _as_int(issue.get("id"))
            exec_result = await _run_fn(execute_phase, inp, issue)
            if not exec_result.get("commits"):
                # Track consecutive zero-commit rounds for this issue.
                # After 2 consecutive failures the pipeline breaks — the
                # issue clearly cannot make progress and will remain
                # agent-ready for a human to pick up (issue #204).
                if issue_id == _zero_commit_issue:
                    _zero_commit_count += 1
                else:
                    _zero_commit_issue = issue_id
                    _zero_commit_count = 1
                if _zero_commit_count >= 2:
                    # Two attempts with zero commits — give up on this
                    # issue and return so the caller can try another.
                    break
                # No commits — skip to next round (execute phase handles
                # failure comments internally).
                continue
            # Reset zero-commit tracking on success.
            _zero_commit_issue = 0
            _zero_commit_count = 0

            review = await _run_fn(review_phase, inp, issue, exec_result)
            verdict = (review or {}).get("verdict")
            fix_passes = 0
            while (
                verdict == "needs_fixes" and fix_passes < inp.review_fix_max_iterations
            ):
                fix_passes += 1
                if not await _run_fn(fix_pass, inp, issue, exec_result, review or {}):
                    break
                review = await _run_fn(review_phase, inp, issue, exec_result)
                verdict = (review or {}).get("verdict")

            await _run_fn(notifier, inp, issue, exec_result)
            queued.append(_as_int(issue.get("id")))
            if verdict:
                verdicts[_as_int(issue.get("id"))] = verdict

            # Notify caller that the round completed successfully.
            if post_round is not None:
                await _run_fn(post_round, issue, exec_result, fix_passes, verdict or "")

        return _devloop_result(
            "completed", queued_for_review=queued, review_verdicts=verdicts
        )
