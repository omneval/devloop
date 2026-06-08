"""Tests for the Temporal Schedule reconciliation in ``schedules.py``.

``ensure_schedules`` runs on every worker startup and must converge an existing
schedule to the code-defined desired state.

Strategy: a fake Temporal client records ``create_schedule`` calls and hands out
a fake schedule handle whose ``update`` captures and invokes the updater, so we
can assert both the create path and the in-place update path without a server.
"""

from __future__ import annotations

import pytest
from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdateInput,
)
from temporalio.service import RPCError, RPCStatusCode

from devloop import schedules
from devloop.dev_loop import DevLoopInput
from devloop.projects import ProjectConfig


def _project(project_id: str = "omneval") -> ProjectConfig:
    return ProjectConfig(
        id=project_id,
        github_url=f"https://github.com/omneval/{project_id}",
        default_branch="main",
        agent_image="ghcr.io/example/agent:sha-abc",
        agent_label="agent-ready",
        omneval_ingest_secret="omneval-ingest",
        github_token_secret="omneval-agent-github-token",
    )


class _FakeHandle:
    """Captures the updater passed to ``ScheduleHandle.update`` and runs it
    against a supplied current schedule (mimicking what the server would feed).

    ``raises`` simulates the ``describe_schedule`` RPC behind ``update`` failing
    (e.g. a timeout on a slow cluster), exercising the best-effort error path."""

    def __init__(self, current: Schedule, raises: Exception | None = None):
        self._current = current
        self._raises = raises
        self.applied: Schedule | None = None

    async def update(self, updater, **kwargs):
        if self._raises is not None:
            raise self._raises
        result = updater(
            ScheduleUpdateInput(description=_FakeDescription(self._current))
        )
        self.applied = result.schedule


class _FakeDescription:
    def __init__(self, schedule: Schedule):
        self.schedule = schedule


class _FakeClient:
    """Records create_schedule calls; create may be configured to raise
    AlreadyRunning so the update path is exercised."""

    def __init__(
        self,
        *,
        already_running: bool = False,
        current: Schedule | None = None,
        update_raises: Exception | None = None,
    ):
        self._already_running = already_running
        self._current = current
        self._update_raises = update_raises
        self.created: list[tuple[str, Schedule]] = []
        self.handles: dict[str, _FakeHandle] = {}

    async def create_schedule(self, schedule_id, schedule, **kwargs):
        if self._already_running:
            raise ScheduleAlreadyRunningError()
        self.created.append((schedule_id, schedule))
        return schedule_id

    def get_schedule_handle(self, schedule_id):
        handle = _FakeHandle(self._current, raises=self._update_raises)
        self.handles[schedule_id] = handle
        return handle


def _desired_schedule(max_questions: int = 42) -> Schedule:
    return Schedule(
        action=ScheduleActionStartWorkflow(
            "DevLoopWorkflow",
            DevLoopInput(project_id="omneval", max_questions_per_phase=max_questions),
            id="devloop-nightly-omneval",
            task_queue="q",
        ),
        spec=ScheduleSpec(),
    )


@pytest.mark.asyncio
async def test_ensure_creates_when_absent():
    client = _FakeClient(already_running=False)
    await schedules._ensure(client, "devloop-nightly-omneval", _desired_schedule())
    assert [sid for sid, _ in client.created] == ["devloop-nightly-omneval"]
    assert client.handles == {}  # update path never touched


@pytest.mark.asyncio
async def test_ensure_updates_when_present_preserving_pause_state():
    # Live schedule the operator has paused (e.g. pause-on-failure) carrying a
    # stale config; the desired schedule has a new one.
    current = Schedule(
        action=ScheduleActionStartWorkflow(
            "DevLoopWorkflow",
            DevLoopInput(project_id="omneval", max_questions_per_phase=9999),
            id="devloop-nightly-omneval",
            task_queue="q",
        ),
        spec=ScheduleSpec(),
        state=ScheduleState(paused=True, note="paused by operator"),
    )
    client = _FakeClient(already_running=True, current=current)

    await schedules._ensure(
        client, "devloop-nightly-omneval", _desired_schedule(max_questions=42)
    )

    assert client.created == []  # create raised AlreadyRunning
    applied = client.handles["devloop-nightly-omneval"].applied
    assert applied is not None
    # New config reached the action args...
    assert applied.action.args[0].max_questions_per_phase == 42
    # ...while operator-owned runtime state was preserved.
    assert applied.state.paused is True
    assert applied.state.note == "paused by operator"


@pytest.mark.asyncio
async def test_ensure_schedules_does_not_create_nightly():
    """ensure_schedules must NOT create any devloop-nightly-* schedules (ADR-0011:
    GitHub webhook is the sole trigger; nightly sweep removed)."""
    client = _FakeClient(already_running=False)

    await schedules.ensure_schedules(client, [_project("omneval")])

    nightly_ids = [
        sid for sid, _ in client.created if sid.startswith("devloop-nightly-")
    ]
    assert nightly_ids == [], f"unexpected nightly schedules created: {nightly_ids}"


@pytest.mark.asyncio
async def test_ensure_schedules_still_creates_weekly_summary():
    """The weekly Summarization schedule must still be created (unaffected by
    removal of the nightly DevLoop sweep)."""
    client = _FakeClient(already_running=False)

    await schedules.ensure_schedules(client, [_project("omneval")])

    weekly_ids = [
        sid for sid, _ in client.created if sid.startswith("summarize-weekly-")
    ]
    assert "summarize-weekly-omneval" in weekly_ids


@pytest.mark.asyncio
async def test_ensure_swallows_update_rpc_failure():
    """A failing ``update`` (e.g. the ``describe_schedule`` RPC behind it timing
    out on a slow cluster) must not propagate — reconciliation is best-effort and
    runs on the critical worker-startup path."""
    client = _FakeClient(
        already_running=True,
        current=_desired_schedule(),
        update_raises=RPCError("Timeout expired", RPCStatusCode.DEADLINE_EXCEEDED, b""),
    )

    # Must not raise.
    await schedules._ensure(client, "devloop-nightly-omneval", _desired_schedule())


@pytest.mark.asyncio
async def test_ensure_schedules_survives_reconciliation_timeout():
    """The worker must boot even when every schedule's reconciliation fails:
    ``ensure_schedules`` returns normally and still attempts each schedule.

    This is the regression behind the homelab crash — before the fix the RPC
    timeout propagated out of ``main`` and CrashLooped the worker."""
    from devloop.summarization import SummarizeInput

    # Build a plausible current schedule for the update path (weekly summary).
    current = Schedule(
        action=ScheduleActionStartWorkflow(
            "SummarizationWorkflow",
            SummarizeInput(project_id="omneval", trigger="weekly"),
            id="summarize-weekly-omneval",
            task_queue="q",
        ),
        spec=ScheduleSpec(),
    )
    client = _FakeClient(
        already_running=True,
        current=current,
        update_raises=RPCError("Timeout expired", RPCStatusCode.DEADLINE_EXCEEDED, b""),
    )

    # Must not raise despite every update timing out.
    await schedules.ensure_schedules(client, [_project("omneval"), _project("devloop")])

    # Only weekly schedules are now attempted (nightly removed).
    assert set(client.handles) == {
        "summarize-weekly-omneval",
        "summarize-weekly-devloop",
    }
    # No nightly handles created.
    assert not any(k.startswith("devloop-nightly-") for k in client.handles)
