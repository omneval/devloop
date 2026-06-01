"""Tests for GitHub activities (issues #20, #22, #23) with a fake httpx client."""

import pytest
from temporalio.testing import ActivityEnvironment

from devloop import github_ops
from devloop.github_ops import (
    CloseIssuesInput,
    FileIssuesInput,
    NewIssue,
    PlanInput,
    close_issues,
    file_issues,
    plan_issues,
)
from devloop.projects import ProjectConfig, _REGISTRY

_PROJECT = ProjectConfig(
    id="omneval",
    github_url="https://github.com/omneval/omneval",
    default_branch="main",
    agent_image="img",
    agent_label="agent-ready",
    discord_channel="agent-approvals",
    omneval_ingest_secret="s",
    github_token_secret="omneval-agent-github-token",
)


@pytest.fixture(autouse=True)
def _registry():
    _REGISTRY.clear()
    _REGISTRY["omneval"] = _PROJECT
    yield
    _REGISTRY.clear()


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
    def __init__(self, get_pages=None, post_capture=None, patch_capture=None):
        self._get_pages = list(get_pages or [])
        self.posts = post_capture if post_capture is not None else []
        self.patches = patch_capture if patch_capture is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        return FakeResp(self._get_pages.pop(0) if self._get_pages else [])

    def post(self, url, json=None):
        self.posts.append((url, json))
        return FakeResp({"number": 901})

    def patch(self, url, json=None):
        self.patches.append((url, json))
        return FakeResp({})


@pytest.mark.asyncio
async def test_plan_issues_orders_and_fetches(monkeypatch):
    pages = [
        [
            {"number": 2, "title": "B", "body": "after #1"},
            {"number": 1, "title": "A", "body": ""},
        ],
        [],
    ]
    monkeypatch.setattr(github_ops, "_client", lambda cfg: FakeClient(get_pages=pages))
    plan = await ActivityEnvironment().run(plan_issues, PlanInput("omneval"))
    assert [i.number for i in plan.issues] == [1, 2]


@pytest.mark.asyncio
async def test_file_issues_applies_agent_label(monkeypatch):
    posts = []
    monkeypatch.setattr(
        github_ops, "_client", lambda cfg: FakeClient(post_capture=posts)
    )
    created = await ActivityEnvironment().run(
        file_issues, FileIssuesInput("omneval", [NewIssue("t", "b")])
    )
    assert created == [901]
    assert posts[0][1]["labels"] == ["agent-ready"]


@pytest.mark.asyncio
async def test_close_issues_comments_then_closes(monkeypatch):
    posts, patches = [], []
    monkeypatch.setattr(
        github_ops,
        "_client",
        lambda cfg: FakeClient(post_capture=posts, patch_capture=patches),
    )
    await ActivityEnvironment().run(
        close_issues, CloseIssuesInput("omneval", [3], comment="merged in abc")
    )
    assert any("comments" in url for url, _ in posts)
    assert patches[0][1]["state"] == "closed"


# --------------------------------------------------------------------------- #
# agent_pr_issue_numbers — planner filter for issues already up for review
# --------------------------------------------------------------------------- #
def test_agent_pr_issue_numbers_parses_agent_branches():
    pulls = [
        {"head": {"ref": "agent/issue-56-fix-project-setting-persistence"}},
        {"head": {"ref": "agent/issue-51"}},
        {"head": {"ref": "feature/unrelated"}},  # not an agent branch → ignored
        {"head": {"ref": "agent/issue-56-dup"}},  # duplicate issue → deduped
        {"head": {}},  # malformed → skipped
        {},  # malformed → skipped
    ]
    assert github_ops.agent_pr_issue_numbers(pulls) == [51, 56]


def test_agent_pr_issue_numbers_empty():
    assert github_ops.agent_pr_issue_numbers([]) == []
