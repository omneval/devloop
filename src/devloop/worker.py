"""Temporal Orchestration Worker entry point.

Runs three things in one asyncio loop:
  1. the /healthz HTTP server (Kubernetes probes)
  2. the FastAPI webhook receiver (GitHub label + AlertManager)
  3. the Temporal worker polling the homelab-orchestration task queue

Workflow definitions are imported here for registration; their non-deterministic
activity dependencies are loaded under the sandbox via imports_passed_through in
the workflow modules themselves.
"""

import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import uvicorn
from temporalio.client import Client
from temporalio.worker import Worker

from .projects import install_registry
from .schedules import ensure_schedules
from .shared import ORCHESTRATION_QUEUE
from .webhook import create_app

# Activities
from .k8s_jobs import (
    answer_agent_job,
    await_agent_job,
    cleanup_agent_job,
    dispatch_agent_job,
)
from .github_ops import (
    close_issues,
    file_issues,
    open_agent_pr_issue_numbers,
    plan_issues,
    post_pr_comments,
)
from .summarize_activities import summarize_changes

# Workflows
from .workflows import NoopWorkflow, noop_activity
from .dev_loop import DevLoopWorkflow
from .summarization import SummarizationWorkflow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

TEMPORAL_HOST = os.getenv("TEMPORAL_HOST", "localhost:7233")
TASK_QUEUE = os.getenv("TASK_QUEUE", ORCHESTRATION_QUEUE)
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8080"))
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8088"))
PROJECTS_FILE = os.getenv("PROJECTS_FILE", "./projects.yaml")

ACTIVITIES = [
    noop_activity,
    dispatch_agent_job,
    answer_agent_job,
    await_agent_job,
    cleanup_agent_job,
    plan_issues,
    post_pr_comments,
    file_issues,
    close_issues,
    open_agent_pr_issue_numbers,
    summarize_changes,
]

WORKFLOWS = [
    NoopWorkflow,
    DevLoopWorkflow,
    SummarizationWorkflow,
]


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A002
        pass


def _start_health_server() -> None:
    HTTPServer(("", HEALTH_PORT), _HealthHandler).serve_forever()


async def main() -> None:
    threading.Thread(target=_start_health_server, daemon=True).start()

    projects = install_registry(PROJECTS_FILE)

    client = await Client.connect(TEMPORAL_HOST)
    await ensure_schedules(client, projects)

    app = create_app(client, projects)
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=WEBHOOK_PORT, log_level="info")
    )

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=WORKFLOWS,
        activities=ACTIVITIES,
    )
    logging.info(
        "Worker polling '%s' on %s; webhooks on :%d",
        TASK_QUEUE,
        TEMPORAL_HOST,
        WEBHOOK_PORT,
    )
    await asyncio.gather(worker.run(), server.serve())


if __name__ == "__main__":
    asyncio.run(main())
