"""Unit tests for devloop.phases.plan — PlanPhase standalone module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from devloop.phases.plan import PlanPhase, PhaseOps


class TestPlanPhase:
    """PlanPhase — lightweight plan_issue activity or full agent job dispatch."""

    @pytest.mark.asyncio
    async def test_webhook_triggered_uses_plan_issue_activity(self) -> None:
        """When triggering_issue > 0, PlanPhase calls plan_issue activity."""
        plan_phase = PlanPhase()
        callbacks = PhaseOps(
            plan_issue=AsyncMock(return_value={"issues": [{"id": "5"}]}),
            drop_issues_in_review=AsyncMock(
                return_value=[{"id": "5"}]  # no issues in review
            ),
        )
        result = await plan_phase.run(
            inp=MagicMock(triggering_issue=5, agent_label="agent-ready"),
            rnd=1,
            callbacks=callbacks,
        )
        callbacks.plan_issue.assert_awaited_once()
        assert result["issues"][0]["id"] == "5"

    @pytest.mark.asyncio
    async def test_backlog_triggers_agent_job_dispatch(self) -> None:
        """When triggering_issue == 0, PlanPhase dispatches a plan agent job."""
        plan_phase = PlanPhase()
        callbacks = PhaseOps(
            dispatch_plan=AsyncMock(
                return_value=MagicMock(plan={"issues": [{"id": "3"}]})
            ),
            drop_issues_in_review=AsyncMock(
                return_value=[{"id": "3"}]  # no issues in review
            ),
        )
        inp = MagicMock(triggering_issue=0, agent_label="agent-ready")
        result = await plan_phase.run(inp=inp, rnd=1, callbacks=callbacks)
        callbacks.dispatch_plan.assert_awaited_once()
        assert result["issues"][0]["id"] == "3"

    @pytest.mark.asyncio
    async def test_drops_issues_in_review(self) -> None:
        """Issues already having open review PRs are filtered out."""
        plan_phase = PlanPhase()
        callbacks = PhaseOps(
            plan_issue=AsyncMock(return_value={"issues": [{"id": "1"}, {"id": "2"}]}),
            drop_issues_in_review=AsyncMock(
                return_value=[{"id": "1"}]  # only id=1 survives
            ),
        )
        result = await plan_phase.run(
            inp=MagicMock(triggering_issue=5),
            rnd=1,
            callbacks=callbacks,
        )
        ids = [i["id"] for i in result["issues"]]
        assert ids == ["1"]

    @pytest.mark.asyncio
    async def test_empty_issues_returns_issues_key(self) -> None:
        """Plan with no issues still returns dict with empty issues list."""
        plan_phase = PlanPhase()
        callbacks = PhaseOps(
            plan_issue=AsyncMock(return_value={"issues": []}),
            drop_issues_in_review=AsyncMock(return_value=[]),
        )
        result = await plan_phase.run(
            inp=MagicMock(triggering_issue=5),
            rnd=1,
            callbacks=callbacks,
        )
        assert result["issues"] == []
