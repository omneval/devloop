"""Tests for the GitHub issue poller.

Focus: a forward that the webhook does *not* accept (non-2xx or transport
error) must leave the issue out of ``seen`` and must not be persisted, so the
next poll cycle retries it. A previous version unconditionally marked every
issue seen, silently dropping issues whenever the worker was down.
"""

import os

# poll.py reads these at import time; set them before importing the module.
os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("WEBHOOK_URL", "http://webhook.test/webhook/github")
os.environ.setdefault("GITHUB_REPO", "omneval/omneval")

import httpx
import pytest

import poll


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    """Minimal stand-in for httpx.AsyncClient used by forward_to_webhook."""

    def __init__(self, *, status_code: int | None = None, raise_exc: bool = False):
        self._status_code = status_code
        self._raise_exc = raise_exc
        self.calls = 0

    async def post(self, *args, **kwargs):
        self.calls += 1
        if self._raise_exc:
            raise httpx.ConnectError("connection refused")
        return _FakeResponse(self._status_code)


ISSUE = {"number": 42, "title": "t", "state": "open"}


async def test_forward_returns_true_on_2xx():
    client = _FakeClient(status_code=200)
    assert await poll.forward_to_webhook(client, ISSUE) is True


@pytest.mark.parametrize("status", [400, 404, 500, 503])
async def test_forward_returns_false_on_non_2xx(status):
    client = _FakeClient(status_code=status)
    assert await poll.forward_to_webhook(client, ISSUE) is False


async def test_forward_returns_false_on_transport_error():
    client = _FakeClient(raise_exc=True)
    assert await poll.forward_to_webhook(client, ISSUE) is False


async def test_failed_forward_is_not_marked_seen_and_not_saved(monkeypatch):
    """A failed forward must not enter ``seen`` and must not persist state."""
    monkeypatch.setattr(poll, "fetch_labeled_issues", lambda client: _async([ISSUE]))
    monkeypatch.setattr(poll, "forward_to_webhook", lambda client, issue: _async(False))
    saved: list = []
    monkeypatch.setattr(poll, "save_state", lambda seen: saved.append(set(seen)))

    seen = await poll.poll_once(set())

    assert 42 not in seen
    assert saved == []  # nothing persisted, so it retries next cycle


async def test_successful_forward_is_marked_seen_and_saved(monkeypatch):
    monkeypatch.setattr(poll, "fetch_labeled_issues", lambda client: _async([ISSUE]))
    monkeypatch.setattr(poll, "forward_to_webhook", lambda client, issue: _async(True))
    saved: list = []
    monkeypatch.setattr(poll, "save_state", lambda seen: saved.append(set(seen)))

    seen = await poll.poll_once(set())

    assert 42 in seen
    assert saved == [{42}]


async def test_mixed_batch_only_persists_successes(monkeypatch):
    issues = [
        {"number": 1, "title": "a", "state": "open"},
        {"number": 2, "title": "b", "state": "open"},
    ]
    monkeypatch.setattr(poll, "fetch_labeled_issues", lambda client: _async(issues))
    # #1 succeeds, #2 fails.
    monkeypatch.setattr(
        poll,
        "forward_to_webhook",
        lambda client, issue: _async(issue["number"] == 1),
    )
    saved: list = []
    monkeypatch.setattr(poll, "save_state", lambda seen: saved.append(set(seen)))

    seen = await poll.poll_once(set())

    assert seen == {1}
    assert saved == [{1}]


async def _async(value):
    return value
