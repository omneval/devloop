"""TDD tests for GitHub issue #108: Phase enum additions, new dataclasses,
create_github_issue and update_github_issue activities."""

from __future__ import annotations

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from devloop import github_ops
from devloop.github import CreateGithubIssueInput, UpdateGithubIssueInput
from devloop.phases.enums import Phase
from devloop.github_ops import create_github_issue, update_github_issue
from devloop.projects import ProjectConfig, _REGISTRY

_PROJECT = ProjectConfig(
    id="omneval",
    github_url="https://github.com/omneval/omneval",
    default_branch="main",
    agent_image="img",
    agent_label="agent-ready",
    omneval_ingest_secret="s",
    github_token_secret="omneval-agent-github-token",
)


@pytest.fixture(autouse=True)
def _registry():
    _REGISTRY.clear()
    _REGISTRY["omneval"] = _PROJECT
    yield
    _REGISTRY.clear()


# --- Phase enum additions --- #


def test_phase_code_quality_scan_value():
    assert Phase.CODE_QUALITY_SCAN == "code_quality_scan"
    assert Phase.CODE_QUALITY_SCAN.value == "code_quality_scan"


def test_phase_code_quality_improve_value():
    assert Phase.CODE_QUALITY_IMPROVE == "code_quality_improve"
    assert Phase.CODE_QUALITY_IMPROVE.value == "code_quality_improve"


# --- CreateGithubIssueInput dataclass --- #


def test_create_github_issue_input_fields():
    from dataclasses import fields

    inp = CreateGithubIssueInput(
        project_id="omneval",
        title="My title",
        body="My body",
        labels=["bug"],
    )
    assert inp.project_id == "omneval"
    assert inp.title == "My title"
    assert inp.body == "My body"
    assert inp.labels == ["bug"]
    field_names = {f.name for f in fields(CreateGithubIssueInput)}
    assert field_names == {"project_id", "title", "body", "labels"}


# --- UpdateGithubIssueInput dataclass --- #


def test_update_github_issue_input_fields():
    from dataclasses import fields

    inp = UpdateGithubIssueInput(project_id="omneval", issue_number=42)
    assert inp.project_id == "omneval"
    assert inp.issue_number == 42
    assert inp.body == ""
    assert inp.state == ""
    field_names = {f.name for f in fields(UpdateGithubIssueInput)}
    assert field_names == {"project_id", "issue_number", "body", "state"}


# --- Fake HTTP helpers --- #


class FakeResp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class FakeClient:
    def __init__(self, post_return=None, patch_capture=None):
        self._post_return = post_return or {"number": 77}
        self.posts = []
        self.patches = patch_capture if patch_capture is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None):
        self.posts.append((url, json))
        return FakeResp(self._post_return)

    def patch(self, url, json=None):
        self.patches.append((url, json))
        return FakeResp({})


class ErrorClient:
    def __init__(self, status_code=404, body="not found"):
        self._status_code = status_code
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _error(self, method, url):
        req = httpx.Request(method, f"https://api.github.com{url}")
        return httpx.Response(self._status_code, request=req, text=self._body)

    def post(self, url, json=None):
        return self._error("POST", url)

    def patch(self, url, json=None):
        return self._error("PATCH", url)


def _async_client_factory(make_client):
    async def _fake(cfg):
        return make_client()

    return _fake


# --- create_github_issue activity tests --- #


def test_create_github_issue_has_activity_defn():
    assert hasattr(create_github_issue, "__temporal_activity_definition"), (
        "create_github_issue is missing @activity.defn"
    )


@pytest.mark.asyncio
async def test_create_github_issue_returns_issue_number(monkeypatch):
    fake = FakeClient(post_return={"number": 42})
    monkeypatch.setattr(github_ops, "_client", _async_client_factory(lambda: fake))
    result = await ActivityEnvironment().run(
        create_github_issue,
        CreateGithubIssueInput(
            project_id="omneval",
            title="Test issue",
            body="Body text",
            labels=["bug", "agent-ready"],
        ),
    )
    assert result == 42


@pytest.mark.asyncio
async def test_create_github_issue_posts_to_correct_endpoint(monkeypatch):
    fake = FakeClient(post_return={"number": 10})
    monkeypatch.setattr(github_ops, "_client", _async_client_factory(lambda: fake))
    await ActivityEnvironment().run(
        create_github_issue,
        CreateGithubIssueInput(
            project_id="omneval",
            title="T",
            body="B",
            labels=["l"],
        ),
    )
    assert len(fake.posts) == 1
    url, payload = fake.posts[0]
    assert "/repos/omneval/omneval/issues" in url
    assert payload["title"] == "T"
    assert payload["body"] == "B"
    assert payload["labels"] == ["l"]


@pytest.mark.asyncio
async def test_create_github_issue_degrades_on_http_error(monkeypatch):
    monkeypatch.setattr(
        github_ops,
        "_client",
        _async_client_factory(lambda: ErrorClient(500, "server error")),
    )
    result = await ActivityEnvironment().run(
        create_github_issue,
        CreateGithubIssueInput(
            project_id="omneval",
            title="T",
            body="B",
            labels=[],
        ),
    )
    assert result == 0


# --- update_github_issue activity tests --- #


def test_update_github_issue_has_activity_defn():
    assert hasattr(update_github_issue, "__temporal_activity_definition"), (
        "update_github_issue is missing @activity.defn"
    )


@pytest.mark.asyncio
async def test_update_github_issue_patches_body(monkeypatch):
    patches = []
    fake = FakeClient(patch_capture=patches)
    monkeypatch.setattr(github_ops, "_client", _async_client_factory(lambda: fake))
    result = await ActivityEnvironment().run(
        update_github_issue,
        UpdateGithubIssueInput(
            project_id="omneval",
            issue_number=55,
            body="Updated body",
        ),
    )
    assert result is None
    assert len(patches) == 1
    url, payload = patches[0]
    assert "/repos/omneval/omneval/issues/55" in url
    assert payload["body"] == "Updated body"
    assert "state" not in payload


@pytest.mark.asyncio
async def test_update_github_issue_patches_state(monkeypatch):
    patches = []
    fake = FakeClient(patch_capture=patches)
    monkeypatch.setattr(github_ops, "_client", _async_client_factory(lambda: fake))
    await ActivityEnvironment().run(
        update_github_issue,
        UpdateGithubIssueInput(
            project_id="omneval",
            issue_number=55,
            state="closed",
        ),
    )
    url, payload = patches[0]
    assert payload["state"] == "closed"
    assert "body" not in payload


@pytest.mark.asyncio
async def test_update_github_issue_patches_both_fields(monkeypatch):
    patches = []
    fake = FakeClient(patch_capture=patches)
    monkeypatch.setattr(github_ops, "_client", _async_client_factory(lambda: fake))
    await ActivityEnvironment().run(
        update_github_issue,
        UpdateGithubIssueInput(
            project_id="omneval",
            issue_number=55,
            body="new body",
            state="closed",
        ),
    )
    url, payload = patches[0]
    assert payload["body"] == "new body"
    assert payload["state"] == "closed"


@pytest.mark.asyncio
async def test_update_github_issue_degrades_on_http_error(monkeypatch):
    monkeypatch.setattr(
        github_ops,
        "_client",
        _async_client_factory(lambda: ErrorClient(404, "not found")),
    )
    # Must not raise
    result = await ActivityEnvironment().run(
        update_github_issue,
        UpdateGithubIssueInput(
            project_id="omneval",
            issue_number=55,
            state="closed",
        ),
    )
    assert result is None
