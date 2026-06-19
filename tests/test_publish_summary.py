"""TDD tests for publish_summary activity (issue #79).

Tests are written BEFORE implementation per TDD approach.

Covers:
- publish_summary creates a GitHub Issue with the correct title / label
- publish_summary POSTs to a webhook URL when SUMMARIZATION_WEBHOOK_URL is set
  (fire-and-forget: webhook failure is logged but does not raise)
- SummarizeInput.trigger only accepts "weekly"
- ensure_schedules skips the summarization schedule when summarization.enabled=False
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock, create_autospec

import pytest

from devloop import github_ops
from devloop.summarization import SummarizeInput


# ---------------------------------------------------------------------------
# SummarizeInput.trigger restricted to "weekly"
# ---------------------------------------------------------------------------


def test_summarize_input_weekly_trigger_accepted():
    """SummarizeInput must accept 'weekly' as a trigger value."""
    inp = SummarizeInput(project_id="omneval", trigger="weekly")
    assert inp.trigger == "weekly"


def test_summarize_input_default_trigger_is_weekly():
    """SummarizeInput default trigger must be 'weekly'."""
    inp = SummarizeInput(project_id="omneval")
    assert inp.trigger == "weekly"


def test_summarize_input_post_merge_trigger_rejected():
    """SummarizeInput must raise ValueError for 'post-merge' trigger."""
    with pytest.raises(ValueError, match="weekly"):
        SummarizeInput(project_id="omneval", trigger="post-merge")


def test_summarize_input_arbitrary_trigger_rejected():
    """SummarizeInput must raise ValueError for any non-'weekly' trigger."""
    with pytest.raises(ValueError, match="weekly"):
        SummarizeInput(project_id="omneval", trigger="manual")


# ---------------------------------------------------------------------------
# publish_summary — importable and is a Temporal activity
# ---------------------------------------------------------------------------


def test_publish_summary_is_importable():
    from devloop.summarize_activities import publish_summary  # noqa: F401


def test_publish_summary_has_activity_defn():
    """publish_summary must be decorated with @activity.defn."""
    from devloop.summarize_activities import publish_summary

    assert hasattr(publish_summary, "__temporal_activity_definition"), (
        "publish_summary is missing @activity.defn"
    )


# ---------------------------------------------------------------------------
# publish_summary — creates a GitHub Issue with correct title / label
# ---------------------------------------------------------------------------


def _make_fake_client(issue_responses=None, label_responses=None):
    """Return a fake HTTP client context manager.

    Records all POST calls so tests can inspect them.
    """
    if issue_responses is None:
        issue_responses = []
    if label_responses is None:
        label_responses = []

    recorded = {"posts": [], "label_checks": []}

    class FakeResponse:
        def __init__(self, status_code=201, json_data=None):
            self.status_code = status_code
            self._json = json_data or {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise Exception(f"HTTP {self.status_code}")

        def json(self):
            return self._json

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, url, **kwargs):
            recorded["label_checks"].append(url)
            # Return 404 so label gets created
            return FakeResponse(status_code=404)

        def post(self, url, json=None, **kwargs):
            recorded["posts"].append({"url": url, "json": json})
            if "/labels" in url and "/issues" not in url:
                return FakeResponse(
                    status_code=201, json_data={"name": "devloop-summary"}
                )
            return FakeResponse(
                status_code=201,
                json_data={
                    "number": 99,
                    "html_url": "https://github.com/x/y/issues/99",
                },
            )

    return FakeClient(), recorded


def _async_client_returning(client):
    """Build a fake replacement for the real ``github_ops._client(cfg, ...)``.

    Uses ``create_autospec`` against the *real* function so the mock's
    signature and async-ness are pinned to the production contract — if
    ``_client`` ever changes shape (extra required arg, sync-to-async, etc.)
    this mock breaks loudly instead of silently passing like a hand-written
    stand-in would (this is exactly how the v0.0.25/v0.0.26 regression where
    callers stopped matching ``_client``'s real signature slipped through).
    """
    mock = create_autospec(github_ops._client)
    mock.return_value = client
    return mock


def _fake_project():
    fake_cfg = MagicMock()
    fake_cfg.github_url = "https://github.com/omneval/omneval"
    fake_cfg.github_token_secret = "omneval-github-token"
    return fake_cfg


def test_publish_summary_creates_github_issue(monkeypatch):
    """publish_summary must create a GitHub Issue with the correct title and label."""
    from devloop.summarize_activities import publish_summary, PublishSummaryInput

    monkeypatch.setattr(
        "devloop.summarize_activities.get_project", lambda pid: _fake_project()
    )
    monkeypatch.delenv("SUMMARIZATION_WEBHOOK_URL", raising=False)

    fake_client, recorded = _make_fake_client()
    monkeypatch.setattr(
        "devloop.summarize_activities._client", _async_client_returning(fake_client)
    )

    inp = PublishSummaryInput(
        project_id="omneval",
        summary="Big changes landed this week.",
        date="2026-06-06",
    )
    asyncio.run(publish_summary(inp))

    issue_posts = [
        p
        for p in recorded["posts"]
        if "/issues" in p["url"] and "/labels" not in p["url"]
    ]
    assert len(issue_posts) == 1, f"expected 1 issue POST, got {issue_posts}"

    post = issue_posts[0]
    assert "[devloop]" in post["json"]["title"]
    assert "omneval" in post["json"]["title"]
    assert "2026-06-06" in post["json"]["title"]
    assert "digest" in post["json"]["title"]
    assert "devloop-summary" in post["json"]["labels"]
    assert "Big changes landed this week." in post["json"]["body"]


def test_publish_summary_creates_label_if_missing(monkeypatch):
    """publish_summary must create the 'devloop-summary' label if it does not exist."""
    from devloop.summarize_activities import publish_summary, PublishSummaryInput

    monkeypatch.setattr(
        "devloop.summarize_activities.get_project", lambda pid: _fake_project()
    )
    monkeypatch.delenv("SUMMARIZATION_WEBHOOK_URL", raising=False)

    fake_client, recorded = _make_fake_client()
    monkeypatch.setattr(
        "devloop.summarize_activities._client", _async_client_returning(fake_client)
    )

    inp = PublishSummaryInput(project_id="omneval", summary="digest", date="2026-06-06")
    asyncio.run(publish_summary(inp))

    label_posts = [
        p
        for p in recorded["posts"]
        if "/labels" in p["url"] and "/issues" not in p["url"]
    ]
    assert len(label_posts) == 1, f"expected 1 label create POST, got {label_posts}"
    assert label_posts[0]["json"]["name"] == "devloop-summary"


def test_publish_summary_skips_label_create_if_exists(monkeypatch):
    """publish_summary must NOT create the label when it already exists (GET 200)."""
    from devloop.summarize_activities import publish_summary, PublishSummaryInput

    monkeypatch.setattr(
        "devloop.summarize_activities.get_project", lambda pid: _fake_project()
    )
    monkeypatch.delenv("SUMMARIZATION_WEBHOOK_URL", raising=False)

    class FakeResponse:
        def __init__(self, status_code=200, json_data=None):
            self.status_code = status_code
            self._json = json_data or {}

        def raise_for_status(self):
            pass

        def json(self):
            return self._json

    recorded = {"posts": []}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, url, **kwargs):
            # Label exists
            return FakeResponse(status_code=200, json_data={"name": "devloop-summary"})

        def post(self, url, json=None, **kwargs):
            recorded["posts"].append({"url": url, "json": json})
            return FakeResponse(
                status_code=201,
                json_data={"number": 7, "html_url": "https://github.com/x/y/issues/7"},
            )

    monkeypatch.setattr(
        "devloop.summarize_activities._client", _async_client_returning(FakeClient())
    )

    inp = PublishSummaryInput(project_id="omneval", summary="digest", date="2026-06-06")
    asyncio.run(publish_summary(inp))

    label_posts = [
        p
        for p in recorded["posts"]
        if "/labels" in p["url"] and "/issues" not in p["url"]
    ]
    assert label_posts == [], "should not create label when it already exists"


# ---------------------------------------------------------------------------
# publish_summary — optional webhook POST
# ---------------------------------------------------------------------------


def test_publish_summary_posts_to_webhook_when_env_set(monkeypatch):
    """publish_summary must POST JSON to SUMMARIZATION_WEBHOOK_URL when set."""
    from devloop.summarize_activities import publish_summary, PublishSummaryInput

    monkeypatch.setattr(
        "devloop.summarize_activities.get_project", lambda pid: _fake_project()
    )
    monkeypatch.setenv("SUMMARIZATION_WEBHOOK_URL", "https://hooks.example.com/devloop")

    fake_client, recorded = _make_fake_client()
    monkeypatch.setattr(
        "devloop.summarize_activities._client", _async_client_returning(fake_client)
    )

    webhook_calls = []

    class FakeWebhookResp:
        def raise_for_status(self):
            pass

    def fake_post(url, json=None, timeout=None):
        webhook_calls.append({"url": url, "json": json})
        return FakeWebhookResp()

    monkeypatch.setattr("httpx.post", fake_post)

    inp = PublishSummaryInput(
        project_id="omneval", summary="Weekly digest.", date="2026-06-06"
    )
    asyncio.run(publish_summary(inp))

    assert len(webhook_calls) == 1
    call = webhook_calls[0]
    assert call["url"] == "https://hooks.example.com/devloop"
    assert call["json"]["project_id"] == "omneval"
    assert call["json"]["summary"] == "Weekly digest."
    assert call["json"]["date"] == "2026-06-06"


def test_publish_summary_no_webhook_when_env_empty(monkeypatch):
    """publish_summary must NOT POST to a webhook when SUMMARIZATION_WEBHOOK_URL is empty."""
    from devloop.summarize_activities import publish_summary, PublishSummaryInput

    monkeypatch.setattr(
        "devloop.summarize_activities.get_project", lambda pid: _fake_project()
    )
    monkeypatch.setenv("SUMMARIZATION_WEBHOOK_URL", "")

    fake_client, recorded = _make_fake_client()
    monkeypatch.setattr(
        "devloop.summarize_activities._client", _async_client_returning(fake_client)
    )

    webhook_calls = []

    def fake_post(url, json=None, timeout=None):
        webhook_calls.append(url)

    monkeypatch.setattr("httpx.post", fake_post)

    inp = PublishSummaryInput(project_id="omneval", summary="digest", date="2026-06-06")
    asyncio.run(publish_summary(inp))

    assert webhook_calls == [], "no webhook call expected when URL is empty"


def test_publish_summary_webhook_failure_logged_not_raised(monkeypatch, caplog):
    """Webhook POST failure must be logged (not raised) — fire-and-forget."""
    from devloop.summarize_activities import publish_summary, PublishSummaryInput

    monkeypatch.setattr(
        "devloop.summarize_activities.get_project", lambda pid: _fake_project()
    )
    monkeypatch.setenv("SUMMARIZATION_WEBHOOK_URL", "https://dead.example.com/hook")

    fake_client, recorded = _make_fake_client()
    monkeypatch.setattr(
        "devloop.summarize_activities._client", _async_client_returning(fake_client)
    )

    def fake_post(url, json=None, timeout=None):
        raise ConnectionError("refused")

    monkeypatch.setattr("httpx.post", fake_post)

    inp = PublishSummaryInput(project_id="omneval", summary="digest", date="2026-06-06")
    # Must not raise
    with caplog.at_level(logging.WARNING):
        asyncio.run(publish_summary(inp))

    assert any("webhook" in r.message.lower() for r in caplog.records), (
        "expected a log message mentioning webhook failure"
    )


# ---------------------------------------------------------------------------
# SummarizationWorkflow calls publish_summary
# ---------------------------------------------------------------------------


def test_summarization_workflow_does_not_call_send_message():
    """SummarizationWorkflow must NOT reference send_message or MESSAGING_QUEUE."""
    import ast
    import pathlib

    src = pathlib.Path("src/devloop/summarization.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Attribute, ast.Name)):
            name = node.attr if isinstance(node, ast.Attribute) else node.id
            assert name not in ("send_message", "MESSAGING_QUEUE"), (
                f"summarization.py still references messaging-bridge-era name '{name}'"
            )


# ---------------------------------------------------------------------------
# ensure_schedules respects summarization.enabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_schedules_skips_summarization_when_disabled():
    """ensure_schedules must NOT create the summarization schedule when enabled=False."""
    from devloop.schedules import ensure_schedules
    from devloop.projects import ProjectConfig

    project = ProjectConfig(
        id="omneval",
        github_url="https://github.com/omneval/omneval",
        default_branch="main",
        agent_image="ghcr.io/example/agent:sha-abc",
        agent_label="agent-ready",
        omneval_ingest_secret="omneval-ingest",
        github_token_secret="omneval-agent-github-token",
    )

    class FakeClient:
        def __init__(self):
            self.created = []

        async def create_schedule(self, schedule_id, schedule, **kwargs):
            self.created.append(schedule_id)

        def get_schedule_handle(self, schedule_id):
            return None

    client = FakeClient()
    await ensure_schedules(client, [project], summarization_enabled=False)

    summary_ids = [sid for sid in client.created if "summarize" in sid]
    assert summary_ids == [], f"expected no summarization schedules, got {summary_ids}"


@pytest.mark.asyncio
async def test_ensure_schedules_creates_summarization_when_enabled():
    """ensure_schedules must create the summarization schedule when enabled=True (default)."""
    from devloop.schedules import ensure_schedules
    from devloop.projects import ProjectConfig

    project = ProjectConfig(
        id="omneval",
        github_url="https://github.com/omneval/omneval",
        default_branch="main",
        agent_image="ghcr.io/example/agent:sha-abc",
        agent_label="agent-ready",
        omneval_ingest_secret="omneval-ingest",
        github_token_secret="omneval-agent-github-token",
    )

    class FakeClient:
        def __init__(self):
            self.created = []

        async def create_schedule(self, schedule_id, schedule, **kwargs):
            self.created.append(schedule_id)

        def get_schedule_handle(self, schedule_id):
            return None

    client = FakeClient()
    await ensure_schedules(client, [project], summarization_enabled=True)

    summary_ids = [sid for sid in client.created if "summarize" in sid]
    assert "summarize-weekly-omneval" in summary_ids


@pytest.mark.asyncio
async def test_ensure_schedules_deletes_existing_when_disabled():
    """ensure_schedules must delete an existing summarization schedule when disabled."""
    from devloop.schedules import ensure_schedules
    from devloop.projects import ProjectConfig

    project = ProjectConfig(
        id="omneval",
        github_url="https://github.com/omneval/omneval",
        default_branch="main",
        agent_image="ghcr.io/example/agent:sha-abc",
        agent_label="agent-ready",
        omneval_ingest_secret="omneval-ingest",
        github_token_secret="omneval-agent-github-token",
    )

    deleted = []

    class FakeHandle:
        def __init__(self, sid):
            self._sid = sid

        async def delete(self):
            deleted.append(self._sid)

        async def update(self, updater, **kwargs):
            pass

    class FakeClient:
        def __init__(self):
            self.created = []

        async def create_schedule(self, schedule_id, schedule, **kwargs):
            self.created.append(schedule_id)

        def get_schedule_handle(self, schedule_id):
            return FakeHandle(schedule_id)

    client = FakeClient()
    await ensure_schedules(client, [project], summarization_enabled=False)

    assert "summarize-weekly-omneval" in deleted, (
        f"expected summarize-weekly-omneval to be deleted, got {deleted}"
    )


# ---------------------------------------------------------------------------
# PublishSummaryInput dataclass
# ---------------------------------------------------------------------------


def test_publish_summary_input_fields():
    """PublishSummaryInput must have project_id, summary, and date fields."""
    from devloop.summarize_activities import PublishSummaryInput

    inp = PublishSummaryInput(project_id="omneval", summary="digest", date="2026-06-06")
    assert inp.project_id == "omneval"
    assert inp.summary == "digest"
    assert inp.date == "2026-06-06"
