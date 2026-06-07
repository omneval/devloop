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
from .shared import JOB_DISPATCH_QUEUE, ORCHESTRATION_QUEUE
from .webhook import create_app

# Activities
from .k8s_jobs import (
    answer_agent_job,
    await_agent_job,
    cleanup_agent_job,
    dispatch_agent_job,
)
from .github_ops import (
    file_issues,
    get_pr_diff,
    open_agent_pr_issue_numbers,
    poll_ci_checks,
    post_github_comment,
    post_pr_comments,
    request_github_reviewer,
)
from .summarize_activities import publish_summary, summarize_changes

# Workflows
from .workflows import NoopWorkflow, noop_activity
from .dev_loop import DevLoopWorkflow
from .pr_comment import PRCommentWorkflow
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

# Summarization schedule config (issue #79) — forwarded from Helm values
# summarization.enabled / summarization.cronSchedule / summarization.webhookUrl.
SUMMARIZATION_ENABLED = os.getenv("SUMMARIZATION_ENABLED", "true").strip().lower() not in (
    "false",
    "0",
    "",
)
SUMMARIZATION_CRON_SCHEDULE = os.getenv("SUMMARIZATION_CRON_SCHEDULE", "")

# Read the max concurrency cap for Agent Execution Job dispatches.
# Malformed or missing value falls back to 1.
try:
    MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "1"))
    if MAX_CONCURRENT_JOBS < 1:
        MAX_CONCURRENT_JOBS = 1
except (ValueError, TypeError):
    MAX_CONCURRENT_JOBS = 1

# Activities that hold inference resources (LLM calls, agent job dispatches).
# These are registered on JOB_DISPATCH_QUEUE so a dedicated Worker can enforce
# a global concurrency cap via max_concurrent_activity_task_executions.
DISPATCH_ACTIVITIES = [
    dispatch_agent_job,
    summarize_changes,
]

# Activities that poll/patch ConfigMaps or perform lightweight GitHub I/O.
# These hold no inference resources and remain on the ORCHESTRATION_QUEUE.
ORCHESTRATION_ACTIVITIES = [
    noop_activity,
    answer_agent_job,
    await_agent_job,
    cleanup_agent_job,
    post_pr_comments,
    post_github_comment,
    file_issues,
    get_pr_diff,
    open_agent_pr_issue_numbers,
    poll_ci_checks,
    request_github_reviewer,
    publish_summary,
]

WORKFLOWS = [
    NoopWorkflow,
    DevLoopWorkflow,
    PRCommentWorkflow,
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
    await ensure_schedules(
        client,
        projects,
        summarization_enabled=SUMMARIZATION_ENABLED,
        summarization_cron_schedule=SUMMARIZATION_CRON_SCHEDULE,
    )

    app = create_app(client, projects)
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=WEBHOOK_PORT, log_level="info")
    )

    orchestration_worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=WORKFLOWS,
        activities=ORCHESTRATION_ACTIVITIES,
    )
    dispatch_worker = Worker(
        client,
        task_queue=JOB_DISPATCH_QUEUE,
        workflows=[],
        activities=DISPATCH_ACTIVITIES,
        max_concurrent_activity_task_executions=MAX_CONCURRENT_JOBS,
    )
    logging.info(
        "Orchestration worker polling '%s' on %s; webhooks on :%d",
        TASK_QUEUE,
        TEMPORAL_HOST,
        WEBHOOK_PORT,
    )
    logging.info(
        "Job-dispatch worker polling '%s' with maxConcurrentJobs=%d",
        JOB_DISPATCH_QUEUE,
        MAX_CONCURRENT_JOBS,
    )
    await asyncio.gather(
        orchestration_worker.run(),
        dispatch_worker.run(),
        server.serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())
