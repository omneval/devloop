"""Tests for the devloop-job-dispatch task queue (issue #73).

Verifies:
1. JOB_DISPATCH_QUEUE constant exists in shared.py with the correct default.
2. worker.py creates two Worker instances — one per task queue.
3. dispatch_agent_job and summarize_changes are on JOB_DISPATCH_QUEUE only.
4. await_agent_job and answer_agent_job are on ORCHESTRATION_QUEUE only.
5. MAX_CONCURRENT_JOBS env var is read correctly with fallback to 1.
6. worker.py exposes max_concurrent_jobs for the dispatch worker.
"""

from __future__ import annotations

import importlib
import os
from unittest import mock

from devloop.shared import JOB_DISPATCH_QUEUE, ORCHESTRATION_QUEUE
from devloop import worker


# ---------------------------------------------------------------------------
# 1. Constant exists in shared.py
# ---------------------------------------------------------------------------


def test_job_dispatch_queue_constant_exists():
    """JOB_DISPATCH_QUEUE must be defined in shared.py."""
    from devloop.shared import JOB_DISPATCH_QUEUE  # noqa: F401

    assert JOB_DISPATCH_QUEUE is not None
    assert isinstance(JOB_DISPATCH_QUEUE, str)
    assert len(JOB_DISPATCH_QUEUE) > 0


def test_job_dispatch_queue_default_value():
    """Default value for JOB_DISPATCH_QUEUE is 'devloop-job-dispatch'."""
    # Reload without the env var set to confirm the default.
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JOB_DISPATCH_QUEUE", None)
        import devloop.shared as shared_mod

        importlib.reload(shared_mod)
        assert shared_mod.JOB_DISPATCH_QUEUE == "devloop-job-dispatch"
    # Reload again to restore module state used by other tests.
    importlib.reload(shared_mod)


def test_job_dispatch_queue_overridable_via_env():
    """JOB_DISPATCH_QUEUE can be overridden via environment variable."""
    with mock.patch.dict(os.environ, {"JOB_DISPATCH_QUEUE": "custom-queue"}):
        import devloop.shared as shared_mod

        importlib.reload(shared_mod)
        assert shared_mod.JOB_DISPATCH_QUEUE == "custom-queue"
    importlib.reload(shared_mod)


def test_orchestration_queue_unchanged():
    """Changing JOB_DISPATCH_QUEUE must not affect ORCHESTRATION_QUEUE."""
    assert ORCHESTRATION_QUEUE != JOB_DISPATCH_QUEUE


# ---------------------------------------------------------------------------
# 2. worker.py exposes two activity lists
# ---------------------------------------------------------------------------


def test_worker_has_dispatch_activities_list():
    """worker.py must export DISPATCH_ACTIVITIES for the job-dispatch Worker."""
    assert hasattr(worker, "DISPATCH_ACTIVITIES")
    assert isinstance(worker.DISPATCH_ACTIVITIES, list)
    assert len(worker.DISPATCH_ACTIVITIES) > 0


def test_worker_has_orchestration_activities_list():
    """worker.py must export ORCHESTRATION_ACTIVITIES for the orchestration Worker."""
    assert hasattr(worker, "ORCHESTRATION_ACTIVITIES")
    assert isinstance(worker.ORCHESTRATION_ACTIVITIES, list)
    assert len(worker.ORCHESTRATION_ACTIVITIES) > 0


# ---------------------------------------------------------------------------
# 3. Activity routing — dispatch_agent_job + summarize_changes on JOB_DISPATCH_QUEUE
# ---------------------------------------------------------------------------


def test_dispatch_agent_job_in_dispatch_activities():
    """dispatch_agent_job must be registered in DISPATCH_ACTIVITIES."""
    from devloop.k8s_jobs import dispatch_agent_job

    assert dispatch_agent_job in worker.DISPATCH_ACTIVITIES


def test_summarize_changes_in_dispatch_activities():
    """summarize_changes must be registered in DISPATCH_ACTIVITIES."""
    from devloop.summarize_activities import summarize_changes

    assert summarize_changes in worker.DISPATCH_ACTIVITIES


def test_dispatch_agent_job_not_in_orchestration_activities():
    """dispatch_agent_job must NOT be in ORCHESTRATION_ACTIVITIES."""
    from devloop.k8s_jobs import dispatch_agent_job

    assert dispatch_agent_job not in worker.ORCHESTRATION_ACTIVITIES


def test_summarize_changes_not_in_orchestration_activities():
    """summarize_changes must NOT be in ORCHESTRATION_ACTIVITIES."""
    from devloop.summarize_activities import summarize_changes

    assert summarize_changes not in worker.ORCHESTRATION_ACTIVITIES


# ---------------------------------------------------------------------------
# 4. Activity routing — await_agent_job + answer_agent_job on ORCHESTRATION_QUEUE
# ---------------------------------------------------------------------------


def test_await_agent_job_in_orchestration_activities():
    """await_agent_job must stay in ORCHESTRATION_ACTIVITIES."""
    from devloop.k8s_jobs import await_agent_job

    assert await_agent_job in worker.ORCHESTRATION_ACTIVITIES


def test_answer_agent_job_in_orchestration_activities():
    """answer_agent_job must stay in ORCHESTRATION_ACTIVITIES."""
    from devloop.k8s_jobs import answer_agent_job

    assert answer_agent_job in worker.ORCHESTRATION_ACTIVITIES


def test_await_agent_job_not_in_dispatch_activities():
    """await_agent_job must NOT be in DISPATCH_ACTIVITIES."""
    from devloop.k8s_jobs import await_agent_job

    assert await_agent_job not in worker.DISPATCH_ACTIVITIES


def test_answer_agent_job_not_in_dispatch_activities():
    """answer_agent_job must NOT be in DISPATCH_ACTIVITIES."""
    from devloop.k8s_jobs import answer_agent_job

    assert answer_agent_job not in worker.DISPATCH_ACTIVITIES


# ---------------------------------------------------------------------------
# 5. MAX_CONCURRENT_JOBS env var reading and fallback
# ---------------------------------------------------------------------------


def test_max_concurrent_jobs_default_is_1():
    """MAX_CONCURRENT_JOBS defaults to 1 when the env var is absent."""
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MAX_CONCURRENT_JOBS", None)
        import devloop.worker as worker_mod

        importlib.reload(worker_mod)
        assert worker_mod.MAX_CONCURRENT_JOBS == 1
    importlib.reload(worker_mod)


def test_max_concurrent_jobs_reads_env_var():
    """MAX_CONCURRENT_JOBS is set from the MAX_CONCURRENT_JOBS env var."""
    with mock.patch.dict(os.environ, {"MAX_CONCURRENT_JOBS": "5"}):
        import devloop.worker as worker_mod

        importlib.reload(worker_mod)
        assert worker_mod.MAX_CONCURRENT_JOBS == 5
    importlib.reload(worker_mod)


def test_max_concurrent_jobs_malformed_falls_back_to_1():
    """A non-integer MAX_CONCURRENT_JOBS falls back to 1."""
    with mock.patch.dict(os.environ, {"MAX_CONCURRENT_JOBS": "not-a-number"}):
        import devloop.worker as worker_mod

        importlib.reload(worker_mod)
        assert worker_mod.MAX_CONCURRENT_JOBS == 1
    importlib.reload(worker_mod)


def test_max_concurrent_jobs_empty_string_falls_back_to_1():
    """An empty MAX_CONCURRENT_JOBS falls back to 1."""
    with mock.patch.dict(os.environ, {"MAX_CONCURRENT_JOBS": ""}):
        import devloop.worker as worker_mod

        importlib.reload(worker_mod)
        assert worker_mod.MAX_CONCURRENT_JOBS == 1
    importlib.reload(worker_mod)


# ---------------------------------------------------------------------------
# 6. worker.py module-level MAX_CONCURRENT_JOBS is accessible
# ---------------------------------------------------------------------------


def test_worker_exposes_max_concurrent_jobs():
    """worker.py must expose MAX_CONCURRENT_JOBS as a module attribute."""
    assert hasattr(worker, "MAX_CONCURRENT_JOBS")
    assert isinstance(worker.MAX_CONCURRENT_JOBS, int)
    assert worker.MAX_CONCURRENT_JOBS >= 1


def test_job_dispatch_queue_name_in_worker():
    """worker.py must import and use JOB_DISPATCH_QUEUE from shared."""
    assert hasattr(worker, "JOB_DISPATCH_QUEUE")


def test_dispatch_worker_concurrency_kwarg_matches_sdk():
    """The kwarg worker.py passes to cap dispatch concurrency must be one
    temporalio.worker.Worker actually accepts — a name drift here raises
    TypeError at startup (caught in real-cluster testing of #73)."""
    import inspect
    from temporalio.worker import Worker

    params = inspect.signature(Worker.__init__).parameters
    assert "max_concurrent_activities" in params
    source = inspect.getsource(worker.main)
    assert "max_concurrent_activities=MAX_CONCURRENT_JOBS" in source
