import sys
from pathlib import Path

# Add repo root to sys.path so scripts/ and src/ are importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import pytest


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
