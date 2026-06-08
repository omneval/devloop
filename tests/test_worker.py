"""Tests for the reference Temporal Orchestration Worker entry point (issue #6).

Verifies the two contract guarantees the devloop-temporal-worker image makes
out of the box, without standing up Temporal:

1. The worker registers DevLoopWorkflow and SummarizationWorkflow on the
   orchestration task queue (so a consumer with no custom workflows still gets
   the Dev Loop and Summarization workflows).
2. The /healthz endpoint returns 200 (Kubernetes liveness/readiness probe).
"""

from __future__ import annotations

import threading
from http.server import HTTPServer

import httpx

from devloop import DevLoopWorkflow, SummarizationWorkflow
from devloop.github_ops import get_pr_diff
from devloop.shared import ORCHESTRATION_QUEUE
from devloop import worker


def test_reference_worker_registers_devloop_and_summarization():
    """The out-of-the-box worker registers both core workflows."""
    assert DevLoopWorkflow in worker.WORKFLOWS
    assert SummarizationWorkflow in worker.WORKFLOWS


def test_get_pr_diff_remains_a_registered_activity():
    """``get_pr_diff`` stays importable from ``devloop.github_ops`` and
    registered on the orchestration queue.

    Issue #99: PRCommentWorkflow stopped calling it (the agent fetches the
    diff itself via ``gh pr diff``, #98), and a follow-up cleanup deleted the
    now-internally-unused activity outright — breaking downstream workers
    that import and register it directly on their own task queues. It's kept
    as a standalone activity for exactly that kind of consumer.
    """
    assert get_pr_diff in worker.ORCHESTRATION_ACTIVITIES


def test_task_queue_defaults_to_orchestration_queue():
    """The worker polls the orchestration task queue by default."""
    assert worker.TASK_QUEUE == ORCHESTRATION_QUEUE


def test_healthz_returns_200():
    """GET /healthz returns 200; any other path returns 404."""
    server = HTTPServer(("127.0.0.1", 0), worker._HealthHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        assert httpx.get(f"{base}/healthz").status_code == 200
        assert httpx.get(f"{base}/other").status_code == 404
    finally:
        server.shutdown()
        thread.join(timeout=5)
