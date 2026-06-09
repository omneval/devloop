"""CodeQualityWorkflow — scheduled sentrux scan with AI-driven improvement issue filing (issue #82)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from ._workflow_common import _RETRY, _GITHUB_COMMENT_TIMEOUT, _WorkflowCommon
    from .shared import (
        CreateGithubIssueInput,
        JOB_DISPATCH_QUEUE,
        ORCHESTRATION_QUEUE,
        Phase,
        TaskSpec,
        UpdateGithubIssueInput,
    )


@dataclass
class CodeQualityInput:
    project_id: str
    threshold: int = 7000       # 0–10000 native sentrux scale
    agent_label: str = "agent-ready"


@workflow.defn
class CodeQualityWorkflow(_WorkflowCommon):
    @workflow.run
    async def run(self, inp: CodeQualityInput) -> None:
        project_id = inp.project_id
        threshold = inp.threshold
        agent_label = inp.agent_label
        date_str = workflow.now().strftime("%Y-%m-%d")

        # Step 1: Open parent issue
        parent_issue_number: int = await workflow.execute_activity(
            "create_github_issue",
            CreateGithubIssueInput(
                project_id=project_id,
                title=f"[devloop] Code quality report — {project_id} — {date_str}",
                body="Sentrux scan in progress…",
                labels=["devloop-code-quality"],
            ),
            result_type=int,
            start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
            retry_policy=_RETRY,
        )

        # Step 2: Post queued comment before scan
        await self._comment(project_id, parent_issue_number, "⏳ queued — sentrux scan starting")

        # Step 3: Dispatch scan phase
        scan_result = await self._dispatch(
            project_id,
            TaskSpec(
                phase=Phase.CODE_QUALITY_SCAN.value,
                project_id=project_id,
                issue_number=parent_issue_number,
                extra={"threshold": threshold},
            ),
            issue_number=parent_issue_number,
        )

        # Step 4: Deserialize result from plan field
        plan = scan_result.plan or {}
        score: int = int(plan.get("score", 0))
        report: str = str(plan.get("report", scan_result.summary))
        scan_error: bool = bool(plan.get("scan_error", False))
        error_message: str = str(plan.get("error_message", ""))

        # Step 5: Abort path — scan error (no rules.toml etc.)
        if scan_error:
            error_body = f"## Scan Error\n\n{error_message or report}"
            await workflow.execute_activity(
                "update_github_issue",
                UpdateGithubIssueInput(
                    project_id=project_id,
                    issue_number=parent_issue_number,
                    body=error_body,
                    state="closed",
                ),
                start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
                retry_policy=_RETRY,
            )
            return

        result_body = f"## Code Quality Report\n\nScore: **{score}** / 10000 (threshold: {threshold})\n\n```\n{report}\n```"

        # Step 6: Pass path — score meets threshold
        if score >= threshold:
            await workflow.execute_activity(
                "update_github_issue",
                UpdateGithubIssueInput(
                    project_id=project_id,
                    issue_number=parent_issue_number,
                    body=result_body,
                ),
                start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
                retry_policy=_RETRY,
            )
            await self._comment(project_id, parent_issue_number, f"✅ Quality check passed: {score}/{threshold}")
            await workflow.execute_activity(
                "update_github_issue",
                UpdateGithubIssueInput(
                    project_id=project_id,
                    issue_number=parent_issue_number,
                    state="closed",
                ),
                start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
                retry_policy=_RETRY,
            )
            return

        # Step 7: Fail path — score below threshold
        await workflow.execute_activity(
            "update_github_issue",
            UpdateGithubIssueInput(
                project_id=project_id,
                issue_number=parent_issue_number,
                body=result_body,
            ),
            start_to_close_timeout=_GITHUB_COMMENT_TIMEOUT,
            retry_policy=_RETRY,
        )
        await self._comment(
            project_id,
            parent_issue_number,
            f"⚠️ Quality below threshold ({score} < {threshold}) — filing improvement issues.",
        )

        # Step 8: Post queued comment before improve phase
        await self._comment(project_id, parent_issue_number, "⏳ queued — filing improvement issues")

        # Step 9: Dispatch improve phase
        improve_result = await self._dispatch(
            project_id,
            TaskSpec(
                phase=Phase.CODE_QUALITY_IMPROVE.value,
                project_id=project_id,
                issue_number=parent_issue_number,
                extra={
                    "sentrux_report": report,
                    "parent_issue_number": parent_issue_number,
                    "agent_label": agent_label,
                },
            ),
            issue_number=parent_issue_number,
        )

        # Step 10: Post completion comment
        await self._comment(
            project_id,
            parent_issue_number,
            f"📋 Filed improvement issues — see sub-issues below.\n\n{improve_result.summary}",
        )
