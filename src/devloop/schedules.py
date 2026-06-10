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
from temporalio.service import RPCError, RPCStatusCode

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


def _default_weekly_spec() -> ScheduleSpec:
    return ScheduleSpec(
        calendars=[
            ScheduleCalendarSpec(
                # Monday = 1 in Temporal's day-of-week range
                day_of_week=[ScheduleRange(1)],
                hour=[ScheduleRange(8)],
                minute=[ScheduleRange(0)],
            )
        ]
    )


def _parse_cron_field(field: str) -> list[ScheduleRange] | None:
    """Parse a single 5-field cron field into ScheduleRanges.

    Supports plain integers and ``*`` (returns ``None``, meaning "every value" —
    omit the constraint so Temporal treats it as unrestricted).  Anything else
    (ranges, steps, lists) is not supported and triggers a fallback to the
    default spec.
    """
    field = field.strip()
    if field == "*":
        return None
    if field.lstrip("-").isdigit():
        return [ScheduleRange(int(field))]
    raise ValueError(f"unsupported cron field {field!r}")


def build_schedule_spec(cron_schedule: str) -> ScheduleSpec:
    """Build a ``ScheduleSpec`` from a 5-field cron string.

    Falls back to the default Monday-08:00 spec when ``cron_schedule`` is empty
    or cannot be parsed (logs a warning in the latter case so misconfiguration
    stays visible without crashing schedule reconciliation).

    Supported cron syntax: plain integers and ``*`` per field
    (``minute hour day-of-month month day-of-week``). Day-of-month and month
    wildcards are required (only weekly, day-of-week-anchored schedules are
    supported); anything richer (ranges, steps, lists) falls back to the default.
    """
    cron_schedule = (cron_schedule or "").strip()
    if not cron_schedule:
        return _default_weekly_spec()

    parts = cron_schedule.split()
    if len(parts) != 5:
        log.warning(
            "summarization.cronSchedule %r is not a 5-field cron expression; "
            "falling back to the default Monday 08:00 schedule",
            cron_schedule,
        )
        return _default_weekly_spec()

    minute, hour, dom, month, dow = parts
    try:
        minute_r = _parse_cron_field(minute)
        hour_r = _parse_cron_field(hour)
        dom_r = _parse_cron_field(dom)
        month_r = _parse_cron_field(month)
        dow_r = _parse_cron_field(dow)
    except ValueError:
        log.warning(
            "summarization.cronSchedule %r uses unsupported cron syntax "
            "(only plain integers and '*' are supported); falling back to the "
            "default Monday 08:00 schedule",
            cron_schedule,
        )
        return _default_weekly_spec()

    if dom_r is not None or month_r is not None:
        log.warning(
            "summarization.cronSchedule %r constrains day-of-month/month, which "
            "is not supported (weekly schedules are day-of-week-anchored); "
            "falling back to the default Monday 08:00 schedule",
            cron_schedule,
        )
        return _default_weekly_spec()

    calendar_kwargs: dict = {}
    if minute_r is not None:
        calendar_kwargs["minute"] = minute_r
    if hour_r is not None:
        calendar_kwargs["hour"] = hour_r
    if dow_r is not None:
        calendar_kwargs["day_of_week"] = dow_r

    return ScheduleSpec(calendars=[ScheduleCalendarSpec(**calendar_kwargs)])


async def _delete_if_exists(client: Client, schedule_id: str) -> None:
    """Delete a schedule if it exists; silently ignore if absent or on error."""
    try:
        await client.get_schedule_handle(schedule_id).delete()
        log.info("deleted schedule %s (disabled via config)", schedule_id)
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return
        log.warning(
            "could not delete schedule %s (ignored)",
            schedule_id,
            exc_info=True,
        )
    except Exception:
        log.warning(
            "could not delete schedule %s (ignored)",
            schedule_id,
            exc_info=True,
        )


async def ensure_schedules(
    client: Client,
    projects: list[ProjectConfig],
    *,
    summarization_enabled: bool = True,
    summarization_cron_schedule: str = "",
    code_quality_enabled: bool = False,
    code_quality_cron_schedule: str = "",
    code_quality_threshold: int = 7000,
    code_quality_agent_label: str = "agent-ready",
) -> None:
    """Reconcile all Temporal Schedules for *projects*.

    Parameters
    ----------
    client:
        Connected Temporal client.
    projects:
        List of enrolled projects from the Project Registry.
    summarization_enabled:
        When ``False`` any existing ``summarize-weekly-*`` schedules are deleted
        and no new ones are created.  Maps to the Helm value
        ``summarization.enabled`` (default ``True``).
    summarization_cron_schedule:
        Optional override for the cron spec (e.g. ``"0 8 * * 1"``).  When
        empty the default Monday-08:00 ``ScheduleCalendarSpec`` is used.  Maps
        to the Helm value ``summarization.cronSchedule``.
    code_quality_enabled:
        When ``True`` a ``code-quality-{project_id}`` schedule is created or
        updated for each project.  When ``False`` any existing schedule is
        deleted.  Maps to the Helm value ``codeQuality.enabled`` (default
        ``False``).
    code_quality_cron_schedule:
        Optional cron spec override for the code quality schedule.  When
        empty the default Monday-08:00 spec is used.  Maps to the Helm value
        ``codeQuality.cronSchedule``.
    code_quality_threshold:
        Sentrux score threshold (0–10000) below which improvement issues are
        filed.  Maps to ``codeQuality.threshold`` (default ``7000``).
    code_quality_agent_label:
        GitHub label applied to filed improvement issues.  Maps to
        ``codeQuality.agentLabel`` (default ``"agent-ready"``).
    """
    from .summarization import SummarizeInput

    for p in projects:
        schedule_id = f"summarize-weekly-{p.id}"

        if not summarization_enabled:
            await _delete_if_exists(client, schedule_id)
        else:
            await _ensure(
                client,
                schedule_id,
                Schedule(
                    action=ScheduleActionStartWorkflow(
                        "SummarizationWorkflow",
                        SummarizeInput(project_id=p.id, trigger="weekly"),
                        id=schedule_id,
                        task_queue=ORCHESTRATION_QUEUE,
                    ),
                    spec=build_schedule_spec(summarization_cron_schedule),
                ),
            )

        # Code quality schedule
        cq_schedule_id = f"code-quality-{p.id}"
        if not code_quality_enabled:
            await _delete_if_exists(client, cq_schedule_id)
        else:
            from .code_quality import CodeQualityInput

            await _ensure(
                client,
                cq_schedule_id,
                Schedule(
                    action=ScheduleActionStartWorkflow(
                        "CodeQualityWorkflow",
                        CodeQualityInput(
                            project_id=p.id,
                            threshold=code_quality_threshold,
                            agent_label=code_quality_agent_label,
                        ),
                        id=cq_schedule_id,
                        task_queue=ORCHESTRATION_QUEUE,
                    ),
                    spec=build_schedule_spec(code_quality_cron_schedule),
                ),
            )
