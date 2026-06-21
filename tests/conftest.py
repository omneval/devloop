"""Shared pytest configuration and helpers for the devloop test suite."""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import pytest
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment

# Prevent the Rust SDK from downloading the Temporal test-server binary from
# dl.temporal.io — on first-run CI runners that URL hangs (issue #204).  The
# Rust SDK ships an embedded dev server that works perfectly for tests and
# requires no network access.  Setting this before any WorkflowEnvironment
# import ensures the SDK picks it up on startup.
os.environ["TEMPORAL_TEST_SERVER_DOWNLOAD"] = "false"

# Add repo root to sys.path so scripts/ and src/ are importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@asynccontextmanager
async def time_skipping_env() -> AsyncIterator[tuple[WorkflowEnvironment, Client]]:
    """Start a time-skipping Temporal test server (no network downloads).

    ``TEMPORAL_TEST_SERVER_DOWNLOAD=false`` is set at module level so the Rust
    SDK uses its embedded dev server instead of trying to download a binary
    from dl.temporal.io (which hangs in CI — issue #204).

    Yields ``(env, client)``.  The env is shut down on context exit.
    """
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env, env.client


@pytest.fixture(autouse=True)
def _patch_github_async_resolve(monkeypatch):
    """Avoid Kubernetes API calls in tests that go through github_ops.

    Many activities call ``_async_resolve(cfg)`` which tries to read a
    ``github_token_secret`` from Kubernetes when GitHub App auth is not
    configured.  Instead of monkeypatching each test, patch ``_async_resolve``
    globally so it returns a fake token.
    """

    async def _fake_async_resolve(cfg, **kwargs):  # noqa: ANN001
        return "fake-token-for-testing"

    from devloop import github_ops

    monkeypatch.setattr(github_ops, "_async_resolve", _fake_async_resolve)