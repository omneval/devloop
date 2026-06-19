"""Unit tests for ``_fetch_changes`` and ``summarize_changes`` (issue #24, #79).

These were the two call sites that broke in v0.0.25/v0.0.26: both called
``github_ops._client`` with the wrong arity/missing ``await`` and shipped
without a single direct test exercising them — ``test_publish_summary.py``
only covered ``publish_summary``. These tests close that gap by mocking
``_client`` with ``create_autospec`` against the real function, so the mock
breaks loudly if a caller ever drifts from ``_client``'s actual signature
again.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, create_autospec

from devloop import github_ops
from devloop.summarization import SummarizeInput


def _autospec_client(client):
    """Autospec'd replacement for ``github_ops._client(cfg, ...)``."""
    mock = create_autospec(github_ops._client)
    mock.return_value = client
    return mock


def _fake_project():
    fake_cfg = MagicMock()
    fake_cfg.github_url = "https://github.com/omneval/omneval"
    fake_cfg.github_token_secret = "omneval-github-token"
    return fake_cfg


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _FakeGithubClient:
    """Routes GET calls to canned responses keyed by URL substring."""

    def __init__(self, latest_commit_sha="headsha", compare_response=None, issues=None):
        self.latest_commit_sha = latest_commit_sha
        self.compare_response = compare_response
        self.issues = issues or {}
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def get(self, url, **kwargs):
        self.calls.append(url)
        if "/compare/" in url:
            if self.compare_response is None:
                return _FakeResponse(status_code=404)
            return _FakeResponse(status_code=200, json_data=self.compare_response)
        if "/issues/" in url:
            number = int(url.rsplit("/", 1)[-1])
            if number in self.issues:
                return _FakeResponse(
                    status_code=200,
                    json_data={"number": number, "title": self.issues[number]},
                )
            return _FakeResponse(status_code=404)
        if url.endswith("/commits"):
            return _FakeResponse(
                status_code=200, json_data=[{"sha": self.latest_commit_sha}]
            )
        raise AssertionError(f"unexpected GET {url}")


# ---------------------------------------------------------------------------
# _fetch_changes
# ---------------------------------------------------------------------------


def test_fetch_changes_resolves_head_when_not_provided(monkeypatch):
    from devloop.summarize_activities import _fetch_changes

    fake_client = _FakeGithubClient(latest_commit_sha="abc123")
    monkeypatch.setattr(
        "devloop.summarize_activities._client", _autospec_client(fake_client)
    )

    commits, issues, head = asyncio.run(
        _fetch_changes(_fake_project(), "omneval/omneval", "", "", [])
    )

    assert head == "abc123"
    assert any(call.endswith("/commits") for call in fake_client.calls)


def test_fetch_changes_skips_resolution_when_head_provided(monkeypatch):
    from devloop.summarize_activities import _fetch_changes

    fake_client = _FakeGithubClient(latest_commit_sha="should-not-be-used")
    monkeypatch.setattr(
        "devloop.summarize_activities._client", _autospec_client(fake_client)
    )

    commits, issues, head = asyncio.run(
        _fetch_changes(_fake_project(), "omneval/omneval", "base123", "head456", [])
    )

    assert head == "head456"
    assert not any(call.endswith("/commits") for call in fake_client.calls), (
        "must not resolve head via /commits when head is already supplied"
    )


def test_fetch_changes_collects_commit_messages_from_compare(monkeypatch):
    from devloop.summarize_activities import _fetch_changes

    fake_client = _FakeGithubClient(
        compare_response={
            "commits": [
                {"commit": {"message": "fix: thing\n\nbody text"}},
                {"commit": {"message": "feat: other thing"}},
            ]
        }
    )
    monkeypatch.setattr(
        "devloop.summarize_activities._client", _autospec_client(fake_client)
    )

    commits, issues, head = asyncio.run(
        _fetch_changes(_fake_project(), "omneval/omneval", "base123", "head456", [])
    )

    assert commits == ["fix: thing", "feat: other thing"]


def test_fetch_changes_no_compare_call_when_base_equals_head(monkeypatch):
    from devloop.summarize_activities import _fetch_changes

    fake_client = _FakeGithubClient()
    monkeypatch.setattr(
        "devloop.summarize_activities._client", _autospec_client(fake_client)
    )

    commits, issues, head = asyncio.run(
        _fetch_changes(_fake_project(), "omneval/omneval", "samesha", "samesha", [])
    )

    assert commits == []
    assert not any("/compare/" in call for call in fake_client.calls)


def test_fetch_changes_resolves_closed_issue_titles(monkeypatch):
    from devloop.summarize_activities import _fetch_changes

    fake_client = _FakeGithubClient(issues={12: "Fix the thing", 34: "Add the other thing"})
    monkeypatch.setattr(
        "devloop.summarize_activities._client", _autospec_client(fake_client)
    )

    commits, issues, head = asyncio.run(
        _fetch_changes(_fake_project(), "omneval/omneval", "base123", "head456", [12, 34, 99])
    )

    assert issues == [
        {"number": 12, "title": "Fix the thing"},
        {"number": 34, "title": "Add the other thing"},
    ], "issue #99 (404) must be skipped, not raise"


# ---------------------------------------------------------------------------
# summarize_changes activity
# ---------------------------------------------------------------------------


def test_summarize_changes_skips_when_no_new_commits_or_issues(monkeypatch):
    from devloop.summarize_activities import summarize_changes

    monkeypatch.setattr(
        "devloop.summarize_activities.get_project", lambda pid: _fake_project()
    )
    monkeypatch.setattr(
        "devloop.summarize_activities.get_last_sha", lambda pid: "samesha"
    )
    fake_client = _FakeGithubClient()
    monkeypatch.setattr(
        "devloop.summarize_activities._client", _autospec_client(fake_client)
    )

    llm_calls = []
    monkeypatch.setattr(
        "devloop.summarize_activities._llm_summary",
        lambda prompt: llm_calls.append(prompt) or "should not be reached",
    )
    set_sha_calls = []
    monkeypatch.setattr(
        "devloop.summarize_activities.set_last_sha",
        lambda pid, sha: set_sha_calls.append((pid, sha)),
    )

    inp = SummarizeInput(project_id="omneval", head_sha="samesha")
    result = asyncio.run(summarize_changes(inp))

    assert result.skipped is True
    assert llm_calls == [], "LLM must not be called when nothing changed"
    assert set_sha_calls == [], "dedup state must not be advanced when skipped"


def test_summarize_changes_publishes_digest_when_new_commits_landed(monkeypatch):
    from devloop.summarize_activities import summarize_changes

    monkeypatch.setattr(
        "devloop.summarize_activities.get_project", lambda pid: _fake_project()
    )
    monkeypatch.setattr(
        "devloop.summarize_activities.get_last_sha", lambda pid: "oldsha"
    )
    fake_client = _FakeGithubClient(
        compare_response={"commits": [{"commit": {"message": "feat: new stuff"}}]}
    )
    monkeypatch.setattr(
        "devloop.summarize_activities._client", _autospec_client(fake_client)
    )
    monkeypatch.setattr(
        "devloop.summarize_activities._llm_summary", lambda prompt: "a tidy digest"
    )
    set_sha_calls = []
    monkeypatch.setattr(
        "devloop.summarize_activities.set_last_sha",
        lambda pid, sha: set_sha_calls.append((pid, sha)),
    )

    inp = SummarizeInput(project_id="omneval", head_sha="newsha")
    result = asyncio.run(summarize_changes(inp))

    assert result.skipped is False
    assert result.summary == "a tidy digest"
    assert result.head_sha == "newsha"
    assert set_sha_calls == [("omneval", "newsha")]
