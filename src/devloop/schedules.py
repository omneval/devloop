"""Temporal Schedules for the Dev Loop nightly sweep and weekly summary.

* Nightly (03:00): start a Dev Loop per enrolled project. The Plan phase no-ops
  cleanly when a project has no open agent-ready issues (issue #20).
* Weekly (Mon 08:00): start a Summarization workflow per project (issue #24).
"""

from __future__ import annotations

import logging

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleSpec,
    ScheduleCalendarSpec,
    ScheduleRange,
)

from .projects import ProjectConfig
from .shared import ORCHESTRATION_QUEUE

log = logging.getLogger(__name__)


async def _ensure(client: Client, schedule_id: str, schedule: Schedule) -> None:
    try:
        await client.create_schedule(schedule_id, schedule)
        log.info("created schedule %s", schedule_id)
    except ScheduleAlreadyRunningError:
        log.info("schedule %s already exists", schedule_id)


async def ensure_schedules(client: Client, projects: list[ProjectConfig]) -> None:
    from .dev_loop import DevLoopInput
    from .summarization import SummarizeInput

    for p in projects:
        await _ensure(
            client,
            f"devloop-nightly-{p.id}",
            Schedule(
                action=ScheduleActionStartWorkflow(
                    "DevLoopWorkflow",
                    DevLoopInput(project_id=p.id, agent_label=p.agent_label),
                    id=f"devloop-nightly-{p.id}",
                    task_queue=ORCHESTRATION_QUEUE,
                ),
                spec=ScheduleSpec(
                    calendars=[
                        ScheduleCalendarSpec(
                            hour=[ScheduleRange(3)],
                            minute=[ScheduleRange(0)],
                        )
                    ]
                ),
            ),
        )
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
