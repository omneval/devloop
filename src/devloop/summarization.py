"""Summarization workflow (issue #24, #79).

Runs on a weekly Temporal Schedule. Reads the changes since the last
summarized commit, asks the LLM for a plain-English digest, and publishes
it as a GitHub Issue on the enrolled repo with a ``devloop-summary`` label.
An optional outbound webhook URL receives the same payload (fire-and-forget).

Sandbox-safe: only stdlib + shared imports here. The I/O (GitHub compare, LLM
call, dedup state, GitHub Issue creation) lives in ``summarize_activities``
and is referenced by name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow

from ._constants import _ACTIVITY_TIMEOUT, _RETRY

with workflow.unsafe.imports_passed_through():
    from .shared import JOB_DISPATCH_QUEUE, ORCHESTRATION_QUEUE

from ..github import PublishSummaryInput


@dataclass
class SummarizeInput:
    project_id: str
    trigger: str = "weekly"  # only "weekly" is supported
    head_sha: str = ""
    closed_issues: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.trigger != "weekly":
            raise ValueError(
                f"SummarizeInput.trigger must be 'weekly'; got {self.trigger!r}. "
                "The 'post-merge' trigger was removed in issue #79 — "
                "SummarizationWorkflow is no longer called from DevLoopWorkflow."
            )


@dataclass
class SummarizeResult:
    skipped: bool = False
    summary: str = ""
    head_sha: str = ""


@workflow.defn
class SummarizationWorkflow:
    @workflow.run
    async def run(self, inp: SummarizeInput) -> SummarizeResult:
        # summarize_changes makes a direct LLM API call — dispatch to
        # JOB_DISPATCH_QUEUE so it respects maxConcurrentJobs.
        result: SummarizeResult = await workflow.execute_activity(
            "summarize_changes",
            inp,
            result_type=SummarizeResult,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=_RETRY,
            task_queue=JOB_DISPATCH_QUEUE,
        )
        if result.skipped:
            workflow.logger.info(
                "summary skipped (no new changes) for %s", inp.project_id
            )
            return result

        # Publish the summary as a GitHub Issue (and optionally to a webhook).
        # workflow.now() is the deterministic, replay-safe wall clock.
        date_str = workflow.now().strftime("%Y-%m-%d")
        await workflow.execute_activity(
            "publish_summary",
            PublishSummaryInput(
                project_id=inp.project_id,
                summary=result.summary,
                date=date_str,
            ),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
            task_queue=ORCHESTRATION_QUEUE,
        )
        workflow.logger.info("summary published for %s (%s)", inp.project_id, date_str)
        return result
