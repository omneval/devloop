"""Summarization workflow tests (issue #24).

Discord delivery has been removed from SummarizationWorkflow per issue #72.
The workflow now logs the summary and returns; delivery is handled separately.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from devloop.shared import ORCHESTRATION_QUEUE
from devloop.summarization import SummarizationWorkflow, SummarizeInput, SummarizeResult


@dataclass
class Mocks:
    result: SummarizeResult = field(
        default_factory=lambda: SummarizeResult(False, "digest", "sha9")
    )
    seen_inputs: list = field(default_factory=list)


M = Mocks()


def _activities():
    @activity.defn(name="summarize_changes")
    async def summarize_changes(inp: SummarizeInput) -> SummarizeResult:
        M.seen_inputs.append(inp)
        return M.result

    return [summarize_changes]


@pytest.fixture
def reset_mocks():
    global M
    M = Mocks()
    return M


async def _run(inp: SummarizeInput):
    acts = _activities()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=ORCHESTRATION_QUEUE,
            workflows=[SummarizationWorkflow],
            activities=acts,
        ):
            return await env.client.execute_workflow(
                SummarizationWorkflow.run,
                inp,
                id=f"sum-{uuid.uuid4().hex[:8]}",
                task_queue=ORCHESTRATION_QUEUE,
            )


@pytest.mark.asyncio
async def test_post_merge_summarizes_changes(reset_mocks):
    """Summarization workflow calls summarize_changes and returns the result."""
    result = await _run(
        SummarizeInput(
            "omneval", trigger="post-merge", head_sha="abc", closed_issues=[1, 2]
        )
    )
    assert result.skipped is False
    assert result.summary == "digest"
    assert M.seen_inputs[0].trigger == "post-merge"


@pytest.mark.asyncio
async def test_weekly_trigger(reset_mocks):
    """Weekly trigger is passed through to the summarize_changes activity."""
    result = await _run(SummarizeInput("omneval", trigger="weekly"))
    assert M.seen_inputs[0].trigger == "weekly"
    assert result.summary == "digest"


@pytest.mark.asyncio
async def test_dedup_skip_returns_skipped(reset_mocks):
    """When summarize_changes returns skipped=True the workflow propagates it."""
    reset_mocks.result = SummarizeResult(skipped=True)
    result = await _run(SummarizeInput("omneval", trigger="weekly"))
    assert result.skipped is True
    assert result.summary == ""
