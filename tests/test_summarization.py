"""Summarization workflow tests (issue #24, #79).

Discord delivery has been removed from SummarizationWorkflow per issue #72.
Per issue #79, delivery is now via a `publish_summary` activity that opens a
GitHub Issue (and optionally POSTs to a webhook). The "post-merge" trigger has
been removed — `SummarizeInput.trigger` now only accepts "weekly".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from devloop.shared import JOB_DISPATCH_QUEUE, ORCHESTRATION_QUEUE, PublishSummaryInput
from devloop.summarization import SummarizationWorkflow, SummarizeInput, SummarizeResult


@dataclass
class Mocks:
    result: SummarizeResult = field(
        default_factory=lambda: SummarizeResult(False, "digest", "sha9")
    )
    seen_inputs: list = field(default_factory=list)
    published: list = field(default_factory=list)


M = Mocks()


def _dispatch_activities():
    @activity.defn(name="summarize_changes")
    async def summarize_changes(inp: SummarizeInput) -> SummarizeResult:
        M.seen_inputs.append(inp)
        return M.result

    return [summarize_changes]


def _orchestration_activities():
    @activity.defn(name="publish_summary")
    async def publish_summary(payload: PublishSummaryInput) -> None:
        M.published.append(
            {
                "project_id": payload.project_id,
                "summary": payload.summary,
                "date": payload.date,
            }
        )

    return [publish_summary]


@pytest.fixture
def reset_mocks():
    global M
    M = Mocks()
    return M


async def _run(inp: SummarizeInput):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=ORCHESTRATION_QUEUE,
            workflows=[SummarizationWorkflow],
            activities=_orchestration_activities(),
        ):
            async with Worker(
                env.client,
                task_queue=JOB_DISPATCH_QUEUE,
                workflows=[],
                activities=_dispatch_activities(),
            ):
                return await env.client.execute_workflow(
                    SummarizationWorkflow.run,
                    inp,
                    id=f"sum-{uuid.uuid4().hex[:8]}",
                    task_queue=ORCHESTRATION_QUEUE,
                )


@pytest.mark.asyncio
async def test_weekly_trigger(reset_mocks):
    """Weekly trigger is passed through to the summarize_changes activity."""
    result = await _run(SummarizeInput("omneval", trigger="weekly"))
    assert M.seen_inputs[0].trigger == "weekly"
    assert result.summary == "digest"


@pytest.mark.asyncio
async def test_weekly_trigger_publishes_summary(reset_mocks):
    """After a successful summary, the workflow calls publish_summary with the
    project_id, summary text, and a date."""
    result = await _run(SummarizeInput("omneval", trigger="weekly"))
    assert result.summary == "digest"
    assert len(M.published) == 1
    payload = M.published[0]
    assert payload["project_id"] == "omneval"
    assert payload["summary"] == "digest"
    assert "date" in payload and payload["date"]


@pytest.mark.asyncio
async def test_dedup_skip_returns_skipped(reset_mocks):
    """When summarize_changes returns skipped=True the workflow propagates it
    and does NOT call publish_summary."""
    reset_mocks.result = SummarizeResult(skipped=True)
    result = await _run(SummarizeInput("omneval", trigger="weekly"))
    assert result.skipped is True
    assert result.summary == ""
    assert M.published == []


def test_summarize_input_rejects_post_merge_trigger():
    """SummarizeInput no longer accepts 'post-merge' (issue #79: SummarizationWorkflow
    is no longer invoked from DevLoopWorkflow as a post-merge child workflow)."""
    with pytest.raises(ValueError):
        SummarizeInput("omneval", trigger="post-merge", head_sha="abc", closed_issues=[1, 2])
