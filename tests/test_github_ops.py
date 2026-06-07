"""Tests for GitHub activities (issues #20, #22, #23) with a fake httpx client."""

import dataclasses

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from devloop import github_ops
from devloop.github_ops import (
    FileIssuesInput,
    NewIssue,
    file_issues,
    poll_ci_checks,
    post_github_comment,
    post_pr_comments,
    request_github_reviewer,
)
from devloop.shared import (
    GithubNotificationInput,
    InlineComment,
    PollCIChecksInput,
    PostCommentsInput,
    RequestReviewerInput,
)
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


def _async_client_factory(make_client):
    """``github_ops._client`` is now ``async def`` (issue #86) — wrap a
    synchronous fake-client factory so ``await _client(cfg)`` resolves to it."""

    async def _fake_client(cfg):
        return make_client()

    return _fake_client


@pytest.mark.asyncio
async def test_file_issues_applies_agent_label(monkeypatch):
    posts = []
    monkeypatch.setattr(
        github_ops, "_client", _async_client_factory(lambda: FakeClient(post_capture=posts))
    )
    created = await ActivityEnvironment().run(
        file_issues, FileIssuesInput("omneval", [NewIssue("t", "b")])
    )
    assert created == [901]
    assert posts[0][1]["labels"] == ["agent-ready"]


@pytest.mark.asyncio
async def test_post_pr_comments_posts_summary(monkeypatch):
    posts = []
    monkeypatch.setattr(
        github_ops, "_client", _async_client_factory(lambda: FakeClient(post_capture=posts))
    )
    await ActivityEnvironment().run(
        post_pr_comments, PostCommentsInput("omneval", 7, "looks good", [])
    )
    assert any("/issues/7/comments" in url for url, _ in posts)
    assert "looks good" in posts[0][1]["body"]


@pytest.mark.asyncio
async def test_post_pr_comments_raises_on_empty_summary_no_inline(monkeypatch):
    """An empty summary with no inline comments must raise — never silently skip."""
    monkeypatch.setattr(github_ops, "_client", _async_client_factory(lambda: FakeClient()))
    with pytest.raises(ValueError, match="summary"):
        await ActivityEnvironment().run(
            post_pr_comments, PostCommentsInput("omneval", 7, "", [])
        )


@pytest.mark.asyncio
async def test_post_pr_comments_raises_on_zero_pr_number(monkeypatch):
    """A pr_number of 0 (unparseable URL) must raise — never silently skip."""
    monkeypatch.setattr(github_ops, "_client", _async_client_factory(lambda: FakeClient()))
    with pytest.raises(ValueError, match="pr_number"):
        await ActivityEnvironment().run(
            post_pr_comments, PostCommentsInput("omneval", 0, "review ok", [])
        )


@pytest.mark.asyncio
async def test_post_pr_comments_posts_inline(monkeypatch):
    posts = []
    pages = [{"head": {"sha": "deadbeef"}}]  # the c.get(/pulls/7) for the commit SHA
    monkeypatch.setattr(
        github_ops,
        "_client",
        _async_client_factory(lambda: FakeClient(get_pages=pages, post_capture=posts)),
    )
    await ActivityEnvironment().run(
        post_pr_comments,
        PostCommentsInput("omneval", 7, "summary", [InlineComment("a.py", 3, "note")]),
    )
    assert any("/pulls/7/reviews" in url for url, _ in posts)


@pytest.mark.asyncio
async def test_post_pr_comments_posts_inline_only_no_summary(monkeypatch):
    """Inline comments alone (no summary) must still be posted."""
    posts = []
    pages = [{"head": {"sha": "deadbeef"}}]
    monkeypatch.setattr(
        github_ops,
        "_client",
        _async_client_factory(lambda: FakeClient(get_pages=pages, post_capture=posts)),
    )
    await ActivityEnvironment().run(
        post_pr_comments,
        PostCommentsInput("omneval", 7, "", [InlineComment("b.py", 10, "fix")]),
    )
    assert any("/pulls/7/reviews" in url for url, _ in posts)


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


# --------------------------------------------------------------------------- #
# Graceful HTTP-error handling (issue #87)
# --------------------------------------------------------------------------- #
class ErrorClient:
    """Fake authed client whose every call returns an HTTP error response —
    used to exercise the degrade-gracefully path on 404/403/etc."""

    def __init__(self, status_code, body="boom"):
        self._status_code = status_code
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _error_response(self, method, url):
        request = httpx.Request(method, f"https://api.github.com{url}")
        return httpx.Response(self._status_code, request=request, text=self._body)

    def get(self, url, params=None):
        return self._error_response("GET", url)

    def post(self, url, json=None):
        return self._error_response("POST", url)


@pytest.mark.asyncio
async def test_post_github_comment_degrades_gracefully_on_404(monkeypatch):
    """A 404 (issue not found / bot not a collaborator) is logged and
    swallowed — not raised — so a transient GitHub-side hiccup doesn't sink
    the whole DevLoopWorkflow round."""
    monkeypatch.setattr(
        github_ops, "_client", _async_client_factory(lambda: ErrorClient(404, "Not Found"))
    )

    # Must not raise.
    await ActivityEnvironment().run(
        post_github_comment,
        GithubNotificationInput(issue_number=7, project_id="omneval", body="hello"),
    )


@pytest.mark.asyncio
async def test_request_github_reviewer_degrades_gracefully_on_403(monkeypatch):
    """A 403 (rate limit / missing permission) is logged and reported as
    'failed' — never raised — and request_github_reviewer's result says so
    rather than claiming success."""
    project_with_reviewer = dataclasses.replace(_PROJECT, pr_reviewer="alice")
    monkeypatch.setattr(github_ops, "get_project", lambda pid: project_with_reviewer)
    monkeypatch.setattr(
        github_ops, "_client", _async_client_factory(lambda: ErrorClient(403, "rate limited"))
    )

    result = await ActivityEnvironment().run(
        request_github_reviewer,
        RequestReviewerInput(project_id="omneval", pr_number=5, reviewer=""),
    )

    assert result.requested is False
    assert result.reason


@pytest.mark.asyncio
async def test_poll_ci_checks_degrades_gracefully_on_404(monkeypatch):
    """A 404 fetching the PR (e.g. it was closed mid-poll) is logged and
    reported as 'pending' — not 'failing' — so _ci_fix_loop waits and
    re-polls instead of mistaking a transient hiccup for a genuine CI
    failure and burning a fix attempt on it."""
    monkeypatch.setattr(
        github_ops, "_client", _async_client_factory(lambda: ErrorClient(404, "Not Found"))
    )

    result = await ActivityEnvironment().run(
        poll_ci_checks,
        PollCIChecksInput(project_id="omneval", pr_number=5),
    )

    assert result.all_passed is False
    assert result.pending is True
    assert result.failures == []
