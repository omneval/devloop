"""Deep modules for webhook event handling (issue #152).

Each handler is a standalone module with a small, explicit interface.
The FastAPI app layer (webhook.create_app) is a thin wrapper that
deserialises the request and calls ``WebhookRouter.route()``.
"""

from __future__ import annotations

import logging
import os
import re

from temporalio.common import WorkflowIDConflictPolicy

from .projects import ProjectConfig, parse_github_repo
from .shared import ORCHESTRATION_QUEUE

log = logging.getLogger(__name__)

# Agent account that posts agent comments/reviews/PRs — events authored by
# this login are the agent's own activity and must not re-trigger
# PRCommentWorkflow.  Kept local to avoid circular import with webhook.py.
AGENT_GITHUB_LOGIN: str = os.environ.get("AGENT_GITHUB_LOGIN", "devloop-bot")

_AGENT_BRANCH = re.compile(r"^agent/issue-(\d+)")


class WorkflowFactory:
    """Builds workflow input objects and starts workflows.

    Takes the Temporal client so the factory can both construct inputs
    and dispatch them — this keeps handler code free of I/O concerns.
    """

    def __init__(self, client) -> None:
        self._client = client

    async def create_devloop_input(
        self,
        project_id: str,
        agent_label: str,
        issue_number,
    ) -> str:
        """Create a DevLoopInput and start the workflow.

        Returns the workflow id so callers can return it in their response dict.
        """
        from .dev_loop import DevLoopInput

        wf_id = f"devloop-{project_id}"
        input_obj = DevLoopInput.from_env(
            project_id, agent_label, int(issue_number or 0)
        )
        await self._client.start_workflow(
            "DevLoopWorkflow",
            input_obj,
            id=wf_id,
            task_queue=ORCHESTRATION_QUEUE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        log.info(
            "triggered Dev Loop %s for %s (issue #%s)",
            wf_id,
            project_id,
            issue_number,
        )
        return wf_id

    async def create_pr_comment_input(
        self,
        project: ProjectConfig,
        *,
        pr_number,
        issue_number,
        branch: str,
        comment_body: str,
        source: str,
        author: str,
    ) -> str:
        """Create a PRCommentInput and start the workflow.

        Returns the workflow id so callers can return it in their response dict.
        """
        repo_slug = parse_github_repo(project.github_url)
        owner, _, name = repo_slug.partition("/")
        wf_id = f"pr-comment-{owner}-{name}-{pr_number}"
        from .pr_comment import PRCommentInput

        input_obj = PRCommentInput.from_env(
            project.id,
            pr_number=int(pr_number or 0),
            issue_number=int(issue_number or 0),
            branch=branch,
            comment_body=comment_body,
            source=source,
            author=author,
        )
        await self._client.start_workflow(
            "PRCommentWorkflow",
            input_obj,
            id=wf_id,
            task_queue=ORCHESTRATION_QUEUE,
            id_conflict_policy=WorkflowIDConflictPolicy.TERMINATE_EXISTING,
        )
        log.info(
            "triggered PRCommentWorkflow %s for %s (PR #%s, source=%s, author=%s)",
            wf_id,
            project.id,
            pr_number,
            source,
            author,
        )
        return wf_id


class IssueLabeledHandler:
    """Handle ``issues`` webhook events with action ``labeled``.

    When a matching label is applied to an issue for a registered project,
    trigger a Dev Loop workflow.
    """

    async def handle(
        self,
        payload: dict,
        by_repo: dict[str, ProjectConfig],
        factory: WorkflowFactory,
    ) -> dict:
        label = (payload.get("label") or {}).get("name", "")
        repo = (payload.get("repository") or {}).get("full_name", "")
        project = by_repo.get(repo)

        if project is None or label != project.agent_label:
            return {"ignored": f"repo={repo} label={label}"}

        issue_number = (payload.get("issue") or {}).get("number")
        wf_id = await factory.create_devloop_input(
            project.id, project.agent_label, issue_number
        )
        return {"workflow_id": wf_id, "project": project.id, "issue": issue_number}


class PRReviewHandler:
    """Handle ``pull_request_review`` webhook events.

    A human's review on an open agent PR re-engages the agent via
    ``PRCommentWorkflow``.  Bot-authored reviews are filtered out.
    """

    async def handle(
        self,
        payload: dict,
        by_repo: dict[str, ProjectConfig],
        factory: WorkflowFactory,
    ) -> dict:
        repo = (payload.get("repository") or {}).get("full_name", "")
        project = by_repo.get(repo)

        if project is None:
            return {"ignored": f"repo={repo}"}

        review = payload.get("review") or {}
        author = (review.get("user") or {}).get("login", "")

        if not author or author == AGENT_GITHUB_LOGIN:
            return {"ignored": f"author={author or '(none)'} (bot or unknown)"}

        pr = payload.get("pull_request") or {}
        head_ref = (pr.get("head") or {}).get("ref", "")
        m = _AGENT_BRANCH.match(head_ref)

        if not m:
            return {"ignored": f"head={head_ref} (not an agent PR)"}

        pr_number = pr.get("number")
        return await _start_pr_comment_workflow(
            project=project,
            pr_number=pr_number,
            issue_number=pr_number,
            branch=head_ref,
            comment_body=(payload.get("review") or {}).get("body") or "",
            source="review",
            author=author,
            factory=factory,
        )


class PRCommentHandler:
    """Handle ``issue_comment`` webhook events.

    A human mentioning the bot on an agent PR re-engages the agent via
    ``PRCommentWorkflow``.  Bot-authored comments are filtered out.
    """

    async def handle(
        self,
        payload: dict,
        by_repo: dict[str, ProjectConfig],
        factory: WorkflowFactory,
    ) -> dict:
        repo = (payload.get("repository") or {}).get("full_name", "")
        project = by_repo.get(repo)

        if project is None:
            return {"ignored": f"repo={repo}"}

        issue = payload.get("issue") or {}

        if not issue.get("pull_request"):
            return {"ignored": "not a PR comment"}

        comment = payload.get("comment") or {}
        author = (comment.get("user") or {}).get("login", "")

        if not author or author == AGENT_GITHUB_LOGIN:
            return {"ignored": f"author={author or '(none)'} (bot or unknown)"}

        body = comment.get("body") or ""

        if f"@{AGENT_GITHUB_LOGIN}" not in body:
            return {"ignored": "no agent mention"}

        pr_number = issue.get("number")
        return await _start_pr_comment_workflow(
            project=project,
            pr_number=pr_number,
            issue_number=pr_number,
            branch="",
            comment_body=body,
            source="comment",
            author=author,
            factory=factory,
        )


class WebhookRouter:
    """Thin dispatcher: route(event_type, payload, by_repo, factory) → dict.

    Knows which handler to call based on the event type.
    """

    async def route(
        self,
        event_type: str,
        payload: dict,
        by_repo: dict[str, ProjectConfig],
        factory: WorkflowFactory,
    ) -> dict:
        if event_type == "issues":
            return await IssueLabeledHandler().handle(payload, by_repo, factory)

        if event_type == "pull_request_review":
            return await PRReviewHandler().handle(payload, by_repo, factory)

        if event_type == "issue_comment":
            return await PRCommentHandler().handle(payload, by_repo, factory)

        return {"ignored": f"event={event_type} action={payload.get('action')}"}


async def _start_pr_comment_workflow(
    *,
    project: ProjectConfig,
    pr_number,
    issue_number,
    branch: str,
    comment_body: str,
    source: str,
    author: str,
    factory: WorkflowFactory,
) -> dict:
    wf_id = await factory.create_pr_comment_input(
        project,
        pr_number=pr_number,
        issue_number=issue_number,
        branch=branch,
        comment_body=comment_body,
        source=source,
        author=author,
    )
    return {
        "workflow_id": wf_id,
        "project": project.id,
        "pr": pr_number,
        "source": source,
    }
