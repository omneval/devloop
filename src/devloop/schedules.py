"""Temporal Schedules for the weekly Summarization workflow.

* Weekly (Mon 08:00): start a Summarization workflow per project (issue #24).

Note: the nightly DevLoop sweep (devloop-nightly-*) was removed in favour of
GitHub webhook ingress as the sole trigger (ADR-0011). GitHub's built-in 3-day
delivery retry makes a polling sweep redundant. Any existing devloop-nightly-*
schedules left in a running Temporal cluster should be deleted manually by the
operator (e.g. ``tctl schedule delete --sid devloop-nightly-<project-id>``).
"""

from __future__ import annotations

import dataclasses
import logging

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleSpec,
    ScheduleCalendarSpec,
    ScheduleRange,
    ScheduleUpdate,
    ScheduleUpdateInput,
)

from .projects import ProjectConfig
from .shared import ORCHESTRATION_QUEUE

log = logging.getLogger(__name__)


async def _ensure(client: Client, schedule_id: str, schedule: Schedule) -> None:
    """Create the schedule, or update an existing one to match ``schedule``.

    ``ensure_schedules`` runs on every worker startup, so updating in place is
    how config changes propagate to scheduled workflows — e.g. a changed cron
    spec or workflow input.  Without this, the first create would win forever
    and later config edits would silently never reach the schedule.

    Code owns the action, spec, and policy; the operator owns runtime state, so
    whether the schedule is paused, its note, and any remaining-action limit are
    read from the live schedule and carried across the update unchanged (a worker
    restart must not silently un-pause a schedule an operator paused).

    Reconciliation is best-effort: it runs on the critical worker-startup path,
    so any failure (e.g. the ``describe_schedule`` RPC behind ``update`` timing
    out on a slow Temporal cluster) is caught and logged rather than allowed to
    crash the worker. The describe is read-only, so a failed update leaves the
    live schedule untouched; convergence simply retries on the next startup. The
    trade-off: while reconciliation keeps failing, code-side config changes
    (e.g. a new gate timeout) won't reach an already-existing schedule — hence
    the loud WARNING so a persistent failure stays visible.
    """
    try:
        try:
            await client.create_schedule(schedule_id, schedule)
            log.info("created schedule %s", schedule_id)
            return
        except ScheduleAlreadyRunningError:
            pass

        def _apply_desired(inp: ScheduleUpdateInput) -> ScheduleUpdate:
            # Pure: the SDK may invoke this multiple times in a conflict-resolution
            # loop. Overwrite everything from the freshly-built ``schedule`` except
            # the operator-controlled runtime state.
            refreshed = dataclasses.replace(
                schedule, state=inp.description.schedule.state
            )
            return ScheduleUpdate(schedule=refreshed)

        await client.get_schedule_handle(schedule_id).update(_apply_desired)
        log.info("updated schedule %s", schedule_id)
    except Exception:
        log.warning(
            "schedule reconciliation for %s failed; worker will continue and "
            "retry on next startup (code-side config changes will not reach this "
            "schedule until reconciliation succeeds)",
            schedule_id,
            exc_info=True,
        )


async def ensure_schedules(client: Client, projects: list[ProjectConfig]) -> None:
    from .summarization import SummarizeInput

    for p in projects:
        await _ensure(
            client,
            f"summarize-weekly-{p.id}",
            Schedule(
                action=ScheduleActionStartWorkflow(
                    "SummarizationWorkflow",
                    SummarizeInput(project_id=p.id, trigger="weekly"),
                    id=f"summarize-weekly-{p.id}",
                    task_queue=ORCHESTRATION_QUEUE,
                ),
                spec=ScheduleSpec(
                    calendars=[
                        ScheduleCalendarSpec(
                            # Monday = 1 in Temporal's day-of-week range
                            day_of_week=[ScheduleRange(1)],
                            hour=[ScheduleRange(8)],
                            minute=[ScheduleRange(0)],
                        )
                    ]
                ),
            ),
        )
