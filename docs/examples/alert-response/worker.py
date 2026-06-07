"""Temporal worker that runs both the SDK workflows and custom AlertResponseWorkflow.

This is the consumer extension pattern: a single worker process registers
workflows from omneval-devloop (DevLoopWorkflow, SummarizationWorkflow) alongside
custom workflows (AlertResponseWorkflow) on the same task queue.

The worker reuses omneval-devloop's activity definitions (dispatch_agent_job,
post_github_comment, etc.) so custom workflows can dispatch Agent Jobs and
post notifications without reimplementing infrastructure code.
"""

import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import uvicorn
from temporalio.client import Client
from temporalio.worker import Worker

from devloop.projects import install_registry
from devloop.schedules import ensure_schedules
from devloop.shared import ORCHESTRATION_QUEUE
from devloop.webhook import create_app

# SDK activities — shared between all workflows on this worker
from devloop.k8s_jobs import (
    answer_agent_job,
    await_agent_job,
    cleanup_agent_job,
    dispatch_agent_job,
)
from devloop.github_ops import (
    file_issues,
    open_agent_pr_issue_numbers,
    post_pr_comments,
)
from devloop.summarize_activities import summarize_changes

# SDK workflows
from devloop.workflows import NoopWorkflow, noop_activity
from devloop.dev_loop import DevLoopWorkflow
from devloop.summarization import SummarizationWorkflow

# Custom workflow
from alert_response import AlertResponseWorkflow

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
    post_pr_comments,
    file_issues,
    open_agent_pr_issue_numbers,
    summarize_changes,
]

WORKFLOWS = [
    NoopWorkflow,
    DevLoopWorkflow,
    SummarizationWorkflow,
    AlertResponseWorkflow,
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
        "Worker polling '%s' on %s; webhooks on :%d; workflows: %s",
        TASK_QUEUE,
        TEMPORAL_HOST,
        WEBHOOK_PORT,
        ", ".join(w.__name__ for w in WORKFLOWS),
    )
    await asyncio.gather(worker.run(), server.serve())


if __name__ == "__main__":
    asyncio.run(main())
