"""Webhook receiver for the Orchestration Worker (issues #20, #25, #31, #78).

A FastAPI app served alongside the Temporal worker:

* ``POST /webhook/github`` — GitHub events:
    - ``issues`` — an ``agent-ready`` label on an issue starts a Dev Loop
      workflow for the matching enrolled project.
    - ``pull_request_review`` / ``issue_comment`` — human feedback on an open
      agent PR (``agent/issue-<N>`` head branch) starts a ``PRCommentWorkflow``
      so the agent can respond (issue #78). Bot-authored events (the agent's
      own comments/reviews) are filtered out via ``AGENT_GITHUB_LOGIN``.
  HMAC-SHA256 signature verification is enforced when ``GITHUB_WEBHOOK_SECRET``
  is set (GitHub sends the ``X-Hub-Signature-256`` header) — both new event
  types go through the same check as ``issues`` events.
* ``POST /alertmanager/webhook`` — AlertManager alerts; each starts an Alert
  Response workflow.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re

from fastapi import FastAPI, Request, Response
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy

from .projects import ProjectConfig, parse_github_repo
from .shared import ORCHESTRATION_QUEUE

log = logging.getLogger(__name__)

# Module-level constants so tests can monkeypatch them on the ``webhook`` module.
GITHUB_WEBHOOK_SECRET: str = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
# The bot account that posts agent comments/reviews/PRs — events authored by
# this login are the agent's own activity and must not re-trigger PRCommentWorkflow.
AGENT_GITHUB_LOGIN: str = os.environ.get("AGENT_GITHUB_LOGIN", "devloop-bot")

# Agent issue branches are named ``agent/issue-<N>[-slug]`` (see entrypoint.py /
# github_ops._AGENT_BRANCH) — PRCommentWorkflow only engages on these PRs.
_AGENT_BRANCH = re.compile(r"^agent/issue-(\d+)")


def _verify_github_signature(body: bytes, signature: str) -> bool:
    """Return True iff the ``X-Hub-Signature-256`` value matches the body HMAC.

    The HMAC is computed over the exact raw request bytes — not re-serialised
    JSON — using ``GITHUB_WEBHOOK_SECRET`` as the key.  Comparison is done with
    ``hmac.compare_digest`` to resist timing attacks.
    """
    expected = hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    # GitHub sends "sha256=<hex>" — strip the prefix before comparing.
    received = signature.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


def create_app(client: Client, projects: list[ProjectConfig]) -> FastAPI:
    app = FastAPI(title="orchestration-worker-webhooks")
    by_repo = {parse_github_repo(p.github_url): p for p in projects}

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/webhook/github")
    async def github_webhook(request: Request):
        # Read raw bytes first so HMAC is computed over the exact wire body.
        body = await request.body()

        if GITHUB_WEBHOOK_SECRET:
            sig = request.headers.get("X-Hub-Signature-256", "")
            if not sig or not _verify_github_signature(body, sig):
                log.warning("GitHub webhook: invalid or missing signature")
                return Response(
                    content='{"detail":"invalid signature"}',
                    status_code=401,
                    media_type="application/json",
                )

        payload = json.loads(body)
        event = request.headers.get("X-GitHub-Event", "")

        if event == "issues" and payload.get("action") == "labeled":
            return await _handle_issue_labeled(payload, by_repo)

        if event == "pull_request_review":
            return await _handle_pull_request_review(payload, by_repo)

        if event == "issue_comment":
            return await _handle_issue_comment(payload, by_repo)

        return {"ignored": f"event={event} action={payload.get('action')}"}

    async def _handle_issue_labeled(payload: dict, by_repo: dict) -> dict:
        label = (payload.get("label") or {}).get("name", "")
        repo = (payload.get("repository") or {}).get("full_name", "")
        project = by_repo.get(repo)
        if project is None or label != project.agent_label:
            return {"ignored": f"repo={repo} label={label}"}

        issue_number = (payload.get("issue") or {}).get("number")
        wf_id = f"devloop-{project.id}"
        await client.start_workflow(
            "DevLoopWorkflow",
            _dev_loop_input(project.id, project.agent_label),
            id=wf_id,
            task_queue=ORCHESTRATION_QUEUE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        log.info(
            "triggered Dev Loop %s for %s (issue #%s)", wf_id, project.id, issue_number
        )
        return {"workflow_id": wf_id, "project": project.id, "issue": issue_number}

    async def _handle_pull_request_review(payload: dict, by_repo: dict) -> dict:
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
            comment_body=review.get("body") or "",
            source="review",
            author=author,
        )

    async def _handle_issue_comment(payload: dict, by_repo: dict) -> dict:
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
        )

    async def _start_pr_comment_workflow(
        *,
        project: ProjectConfig,
        pr_number,
        issue_number,
        branch: str,
        comment_body: str,
        source: str,
        author: str,
    ) -> dict:
        repo_slug = parse_github_repo(project.github_url)
        owner, _, name = repo_slug.partition("/")
        wf_id = f"pr-comment-{owner}-{name}-{pr_number}"
        await client.start_workflow(
            "PRCommentWorkflow",
            _pr_comment_input(
                project.id,
                pr_number=pr_number,
                issue_number=issue_number,
                branch=branch,
                comment_body=comment_body,
                source=source,
                author=author,
            ),
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
        return {
            "workflow_id": wf_id,
            "project": project.id,
            "pr": pr_number,
            "source": source,
        }

    return app


# Inputs are built lazily to avoid importing the workflow modules (and their
# passthrough deps) at module import time in the webhook process path.
def _dev_loop_input(project_id: str, agent_label: str):
    from .dev_loop import DevLoopInput

    return DevLoopInput.from_env(project_id, agent_label)


def _pr_comment_input(
    project_id: str,
    *,
    pr_number,
    issue_number,
    branch: str,
    comment_body: str,
    source: str,
    author: str,
):
    from .pr_comment import PRCommentInput

    return PRCommentInput.from_env(
        project_id,
        pr_number=int(pr_number or 0),
        issue_number=int(issue_number or 0),
        branch=branch,
        comment_body=comment_body,
        source=source,
        author=author,
    )
