"""Webhook receiver for the Orchestration Worker (issues #20, #25, #31).

A FastAPI app served alongside the Temporal worker:

* ``POST /webhook/github`` — GitHub ``issues`` events; an ``agent-ready`` label
  on an issue starts a Dev Loop workflow for the matching enrolled project.
  HMAC-SHA256 signature verification is enforced when ``GITHUB_WEBHOOK_SECRET``
  is set (GitHub sends the ``X-Hub-Signature-256`` header).
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
from temporalio.common import WorkflowIDConflictPolicy

from .projects import ProjectConfig, parse_github_repo
from .shared import ORCHESTRATION_QUEUE

log = logging.getLogger(__name__)

# Module-level constant so tests can monkeypatch ``webhook.GITHUB_WEBHOOK_SECRET``.
GITHUB_WEBHOOK_SECRET: str = os.environ.get("GITHUB_WEBHOOK_SECRET", "")


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
        if event != "issues" or payload.get("action") != "labeled":
            return {"ignored": f"event={event} action={payload.get('action')}"}

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

    return app


# Inputs are built lazily to avoid importing the workflow modules (and their
# passthrough deps) at module import time in the webhook process path.
def _dev_loop_input(project_id: str, agent_label: str):
    from .dev_loop import DevLoopInput

    return DevLoopInput(project_id=project_id, agent_label=agent_label)
