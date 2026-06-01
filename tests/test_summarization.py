"""Summarization workflow tests (issue #24)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from devloop.shared import (
    DISCORD_QUEUE,
    ORCHESTRATION_QUEUE,
    SendMessageInput,
    SendMessageOutput,
)
from devloop.summarization import SummarizationWorkflow, SummarizeInput, SummarizeResult


@dataclass
class Mocks:
    result: SummarizeResult = field(
        default_factory=lambda: SummarizeResult(False, "digest", "sha9")
    )
    seen_inputs: list = field(default_factory=list)
    changelog_posts: list = field(default_factory=list)


M = Mocks()


def _activities():
    @activity.defn(name="summarize_changes")
    async def summarize_changes(inp: SummarizeInput) -> SummarizeResult:
        M.seen_inputs.append(inp)
        return M.result

    @activity.defn(name="send_message")
    async def send_message(inp: SendMessageInput) -> SendMessageOutput:
        M.changelog_posts.append((inp.channel, inp.message, inp.thread_name))
        return SendMessageOutput(thread_id="t")

    return [summarize_changes], [send_message]


@pytest.fixture
def reset_mocks():
    global M
    M = Mocks()
    return M


async def _run(inp: SummarizeInput):
    orch, disc = _activities()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with (
            Worker(
                env.client,
                task_queue=ORCHESTRATION_QUEUE,
                workflows=[SummarizationWorkflow],
                activities=orch,
            ),
            Worker(env.client, task_queue=DISCORD_QUEUE, activities=disc),
        ):
            return await env.client.execute_workflow(
                SummarizationWorkflow.run,
                inp,
                id=f"sum-{uuid.uuid4().hex[:8]}",
                task_queue=ORCHESTRATION_QUEUE,
            )


@pytest.mark.asyncio
async def test_post_merge_posts_to_changelog(reset_mocks):
    result = await _run(
        SummarizeInput(
            "omneval", trigger="post-merge", head_sha="abc", closed_issues=[1, 2]
        )
    )
    assert result.skipped is False
    assert M.changelog_posts, "expected a #changelog post"
    channel, message, title = M.changelog_posts[0]
    assert channel == "changelog"
    assert message == "digest"
    assert "post-merge" in title


@pytest.mark.asyncio
async def test_weekly_trigger(reset_mocks):
    await _run(SummarizeInput("omneval", trigger="weekly"))
    assert M.seen_inputs[0].trigger == "weekly"
    assert "weekly" in M.changelog_posts[0][2]


@pytest.mark.asyncio
async def test_dedup_skip_does_not_post(reset_mocks):
    reset_mocks.result = SummarizeResult(skipped=True)
    result = await _run(SummarizeInput("omneval", trigger="weekly"))
    assert result.skipped is True
    assert M.changelog_posts == []  # nothing new -> no changelog spam
