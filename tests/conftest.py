"""Shared pytest configuration and helpers for the devloop test suite."""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import pytest
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment

# Add repo root to sys.path so scripts/ and src/ are importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Cache directory for the Temporal test server binary.  The Rust SDK stores the
# downloaded binary here and reuses it across runs — the only thing that matters
# is that the path is consistent within a single CI run.  The Rust SDK handles
# version-matching itself (sdk-python SDK version → binary version).
_TEMPORAL_TEST_SERVER_CACHE_DIR: str = os.path.join(
    os.path.dirname(__file__), "..", ".temporal-cache"
)


@asynccontextmanager
async def time_skipping_env(
    client_factory=None,
) -> AsyncIterator[tuple[WorkflowEnvironment, Client]]:
    """Start a time-skipping Temporal test server with binary caching.

    The ``download_dest_dir`` parameter is set to a persistent directory so the
    Rust SDK caches the test-server binary between runs.  In CI, this directory
    is also preserved by the GitHub Actions cache step so the binary survives
    between runner spins (issue #204).

    Yields ``(env, client)``.  The env is shut down on context exit.
    """
    os.makedirs(_TEMPORAL_TEST_SERVER_CACHE_DIR, exist_ok=True)
    async with await WorkflowEnvironment.start_time_skipping(
        download_dest_dir=_TEMPORAL_TEST_SERVER_CACHE_DIR
    ) as env:
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