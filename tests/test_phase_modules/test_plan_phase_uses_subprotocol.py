"""Tests verifying PlanPhase uses its PlanPhaseOps sub-protocol."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from devloop.phases.plan import PlanPhase
from devloop.phases.phase_ops import PhaseOps


async def _drop_issues_empty(
    inp: Any, issues: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """No-op drop: keep all issues."""
    return list(issues)


class TestPlanPhaseUsesPlanOpsSubProtocol:
    """PlanPhase must use its focused PlanPhaseOps sub-protocol."""

    @pytest.mark.asyncio
    async def test_uses_plan_ops_for_plan_issue(
        self,
    ) -> None:
        """PlanPhase accesses plan_issue via plan_ops sub-protocol."""
        phase = PlanPhase()

        async def _plan_issue(inp: Any) -> dict:
            _plan_issue._called = inp
            return {"issues": [{"id": 1}]}

        _plan_issue._called = False  # type: ignore[attr-defined]

        callbacks = PhaseOps()
        callbacks.plan_ops.plan_issue = _plan_issue  # type: ignore[assignment]
        callbacks.plan_ops.comment = AsyncMock()
        callbacks.plan_ops.drop_issues_in_review = _drop_issues_empty

        inp = MagicMock(triggering_issue=42, project_id="test", agent_label="agent")

        result = await phase.run(
            inp=inp,
            rnd=1,
            callbacks=callbacks,
        )

        assert _plan_issue._called is not False  # type: ignore[attr-defined]
        assert result is not None
        assert callbacks.plan_ops.plan_issue is not None

    @pytest.mark.asyncio
    async def test_uses_plan_ops_for_dispatch_plan(
        self,
    ) -> None:
        """PlanPhase accesses dispatch_plan via plan_ops sub-protocol."""
        phase = PlanPhase()

        async def _dispatch_plan(project_id: str, spec: Any, poll: float) -> Any:
            _dispatch_plan._called = (project_id, poll)
            return MagicMock(plan={"issues": [{"id": 1}]})

        _dispatch_plan._called = False  # type: ignore[attr-defined]

        callbacks = PhaseOps()
        callbacks.plan_ops.dispatch_plan = _dispatch_plan  # type: ignore[assignment]
        callbacks.plan_ops.comment = AsyncMock()
        callbacks.plan_ops.drop_issues_in_review = _drop_issues_empty

        inp = MagicMock(
            triggering_issue=0,  # Backlog path
            project_id="test",
            agent_label="agent",
            poll_interval_seconds=5.0,
        )

        result = await phase.run(
            inp=inp,
            rnd=1,
            callbacks=callbacks,
        )

        assert _dispatch_plan._called is not False  # type: ignore[attr-defined]
        assert result is not None
        assert callbacks.plan_ops.dispatch_plan is not None

    @pytest.mark.asyncio
    async def test_uses_plan_ops_for_drop_issues_in_review(
        self,
    ) -> None:
        """PlanPhase accesses drop_issues_in_review via plan_ops sub-protocol."""
        phase = PlanPhase()

        async def _plan_issue(inp: Any) -> dict:
            return {"issues": [{"id": 1}, {"id": 2}]}

        async def _drop_issues(
            inp: Any, issues: list[dict[str, Any]]
        ) -> list[dict[str, Any]]:
            _drop_issues._called = (inp, issues)
            return [i for i in issues if i["id"] == 1]

        _drop_issues._called = False  # type: ignore[attr-defined]

        callbacks = PhaseOps()
        callbacks.plan_ops.plan_issue = _plan_issue
        callbacks.plan_ops.drop_issues_in_review = _drop_issues  # type: ignore[assignment]
        callbacks.plan_ops.comment = AsyncMock()

        inp = MagicMock(triggering_issue=42, project_id="test", agent_label="agent")

        result = await phase.run(
            inp=inp,
            rnd=1,
            callbacks=callbacks,
        )

        assert _drop_issues._called is not False  # type: ignore[attr-defined]
        assert result is not None
        assert result.get("issues") == [{"id": 1}]

    @pytest.mark.asyncio
    async def test_plan_ops_fallback_uses_phaseops_field(self) -> None:
        """When plan_ops.plan_issue is None, PlanPhase falls back to PhaseOps.plan_issue."""
        phase = PlanPhase()

        async def _plan_issue(inp: Any) -> dict:
            _plan_issue._called = True
            return {"issues": [{"id": 99}]}

        _plan_issue._called = False  # type: ignore[attr-defined]

        callbacks = PhaseOps(plan_issue=_plan_issue)
        # plan_ops.plan_issue is None (default), so it should fall back.
        callbacks.plan_ops.plan_issue = None
        callbacks.plan_ops.drop_issues_in_review = _drop_issues_empty

        inp = MagicMock(triggering_issue=42, project_id="test", agent_label="agent")

        result = await phase.run(
            inp=inp,
            rnd=1,
            callbacks=callbacks,
        )

        assert _plan_issue._called is True  # type: ignore[attr-defined]
        assert result is not None
        assert callbacks.plan_ops.plan_issue is None
