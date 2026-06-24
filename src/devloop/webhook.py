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

from fastapi import FastAPI, Request, Response
from temporalio.client import Client

from . import _constants
from .projects import ProjectConfig, parse_github_repo

log = logging.getLogger(__name__)

# Module-level constants so tests can monkeypatch them on the ``webhook`` module.
GITHUB_WEBHOOK_SECRET: str = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
# The bot account that posts agent comments/reviews/PRs — events authored by
# this login are the agent's own activity and must not re-trigger PRCommentWorkflow.
AGENT_GITHUB_LOGIN: str = _constants.AGENT_GITHUB_LOGIN


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
    """Build the webhook FastAPI application.

    All event-handling logic lives in deep modules (``webhook_deep``).
    This function is thin: it wires routes, deserialises requests, and
    delegates to ``WebhookRouter.route()``.
    """
    from .webhook_deep import WebhookRouter, WorkflowFactory

    app = FastAPI(title="orchestration-worker-webhooks")
    by_repo = {parse_github_repo(p.github_url): p for p in projects}
    factory = WorkflowFactory(client)
    router = WebhookRouter()

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

        # Only "issues" events with action "labeled" route to IssueLabeledHandler.
        if event == "issues" and payload.get("action") == "labeled":
            return await router.route(event, payload, by_repo, factory)

        if event == "pull_request_review":
            return await router.route(event, payload, by_repo, factory)

        if event == "issue_comment":
            return await router.route(event, payload, by_repo, factory)

        return {"ignored": f"event={event} action={payload.get('action')}"}

    return app


# Inputs are built lazily to avoid importing the workflow modules (and their
# passthrough deps) at module import time in the webhook process path.
def _dev_loop_input(project_id: str, agent_label: str, triggering_issue=None):
    from .dev_loop import DevLoopInput

    try:
        issue = int(triggering_issue or 0)
    except (TypeError, ValueError):
        issue = 0
    return DevLoopInput.from_env(project_id, agent_label, issue)


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
