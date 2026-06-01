"""Summarization workflow (issue #24).

Runs after a successful Merge (as a Dev Loop child workflow) and on a weekly
Temporal Schedule. Reads the changes since the last summarized commit, asks the
LLM for a plain-English digest, and posts it to ``#changelog``.

Sandbox-safe: only stdlib + shared imports here. The I/O (GitHub compare, LLM
call, dedup state) lives in ``summarize_activities`` and is referenced by name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from .shared import CHANNEL_CHANGELOG, DISCORD_QUEUE, SendMessageInput

_RETRY = RetryPolicy(maximum_attempts=3)


@dataclass
class SummarizeInput:
    project_id: str
    trigger: str = "post-merge"  # post-merge | weekly
    head_sha: str = ""
    closed_issues: list[int] = field(default_factory=list)


@dataclass
class SummarizeResult:
    skipped: bool = False
    summary: str = ""
    head_sha: str = ""


@workflow.defn
class SummarizationWorkflow:
    @workflow.run
    async def run(self, inp: SummarizeInput) -> SummarizeResult:
        result: SummarizeResult = await workflow.execute_activity(
            "summarize_changes",
            inp,
            result_type=SummarizeResult,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=_RETRY,
        )
        if result.skipped:
            workflow.logger.info(
                "summary skipped (no new changes) for %s", inp.project_id
            )
            return result

        title = f"{inp.project_id} — {inp.trigger} digest"
        await workflow.execute_activity(
            "send_message",
            SendMessageInput(
                workflow_id=workflow.info().workflow_id,
                message=result.summary,
                channel=CHANNEL_CHANGELOG,
                thread_name=title,
            ),
            task_queue=DISCORD_QUEUE,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_RETRY,
        )
        return result
