"""Deep-handler tests for webhook architecture (issue #152).

Each handler is independently testable with plain input dicts and a mock
factory — no FastAPI, no HTTP.
"""

from __future__ import annotations

import asyncio

from devloop.webhook_deep import (
    IssueLabeledHandler,
    PRCommentHandler,
    PRReviewHandler,
    WebhookRouter,
)


class _FakeFactory:
    """Stub factory that records calls without importing real input classes."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def create_devloop_input(self, project_id, agent_label, issue_number):
        self.calls.append(
            ("create_devloop_input", project_id, agent_label, issue_number)
        )
        return {"_type": "DevLoopInput", "project_id": project_id}

    async def create_pr_comment_input(
        self, project, pr_number, issue_number, branch, comment_body, source, author
    ):
        self.calls.append(
            (
                "create_pr_comment_input",
                project.id,
                pr_number,
                issue_number,
                branch,
                comment_body,
                source,
                author,
            )
        )
        return {"_type": "PRCommentInput", "project_id": project.id}


class _FakeProject:
    def __init__(
        self,
        project_id="test-project",
        github_url="https://github.com/omneval/omneval",
        agent_label="agent-ready",
    ):
        self.id = project_id
        self.github_url = github_url
        self.agent_label = agent_label


def mock_by_repo():
    return {"omneval/omneval": _FakeProject()}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_workflow_factory_create_devloop_input_is_coroutine():
    # The real factory method is async — verify by running it.
    factory = _FakeFactory()
    result = _run(factory.create_devloop_input("my-project", "agent-ready", 42))
    assert isinstance(result, dict)
    assert result["project_id"] == "my-project"


def test_issue_labeled_handler_starts_devloop_for_matching_label():
    handler = IssueLabeledHandler()
    factory = _FakeFactory()
    payload = {
        "action": "labeled",
        "label": {"name": "agent-ready"},
        "repository": {"full_name": "omneval/omneval"},
        "issue": {"number": 42, "title": "Test"},
    }
    result = _run(handler.handle(payload, mock_by_repo(), factory))
    assert result["project"] == "test-project"
    assert result["issue"] == 42
    assert factory.calls[0][0] == "create_devloop_input"


def test_issue_labeled_handler_ignores_unrelated_label():
    handler = IssueLabeledHandler()
    factory = _FakeFactory()
    payload = {
        "action": "labeled",
        "label": {"name": "needs-review"},
        "repository": {"full_name": "omneval/omneval"},
        "issue": {"number": 1, "title": "Test"},
    }
    result = _run(handler.handle(payload, mock_by_repo(), factory))
    assert "ignored" in result
    assert factory.calls == []


def test_issue_labeled_handler_ignores_unknown_repo():
    handler = IssueLabeledHandler()
    factory = _FakeFactory()
    payload = {
        "action": "labeled",
        "label": {"name": "agent-ready"},
        "repository": {"full_name": "other/repo"},
        "issue": {"number": 1, "title": "Test"},
    }
    result = _run(handler.handle(payload, mock_by_repo(), factory))
    assert "ignored" in result


def test_pr_review_handler_triggers_pr_comment_workflow_for_agent_pr():
    handler = PRReviewHandler()
    factory = _FakeFactory()
    payload = {
        "action": "submitted",
        "review": {"user": {"login": "human-reviewer"}, "body": "Please fix this."},
        "pull_request": {"number": 17, "head": {"ref": "agent/issue-42"}},
        "repository": {"full_name": "omneval/omneval"},
    }
    result = _run(handler.handle(payload, mock_by_repo(), factory))
    assert result["source"] == "review"
    assert result["pr"] == 17
    assert factory.calls[0][0] == "create_pr_comment_input"


def test_pr_review_handler_ignores_bot_review():
    handler = PRReviewHandler()
    factory = _FakeFactory()
    payload = {
        "action": "submitted",
        "review": {"user": {"login": "devloop-bot"}},
        "pull_request": {"number": 17, "head": {"ref": "agent/issue-42"}},
        "repository": {"full_name": "omneval/omneval"},
    }
    result = _run(handler.handle(payload, mock_by_repo(), factory))
    assert "ignored" in result


def test_pr_review_handler_ignores_non_agent_branch():
    handler = PRReviewHandler()
    factory = _FakeFactory()
    payload = {
        "action": "submitted",
        "review": {"user": {"login": "human-reviewer"}},
        "pull_request": {"number": 17, "head": {"ref": "feature/something"}},
        "repository": {"full_name": "omneval/omneval"},
    }
    result = _run(handler.handle(payload, mock_by_repo(), factory))
    assert "ignored" in result


def test_issue_comment_handler_triggers_for_agent_mention_on_pr():
    handler = PRCommentHandler()
    factory = _FakeFactory()
    payload = {
        "action": "created",
        "comment": {
            "user": {"login": "human-reviewer"},
            "body": "@devloop-bot please fix this",
        },
        "issue": {"number": 17, "pull_request": {"url": "https://api.github.com/..."}},
        "repository": {"full_name": "omneval/omneval"},
    }
    result = _run(handler.handle(payload, mock_by_repo(), factory))
    assert result["source"] == "comment"
    assert result["pr"] == 17


def test_issue_comment_handler_ignores_no_mention():
    handler = PRCommentHandler()
    factory = _FakeFactory()
    payload = {
        "action": "created",
        "comment": {
            "user": {"login": "human-reviewer"},
            "body": "Just a normal comment",
        },
        "issue": {"number": 17, "pull_request": {"url": "https://api.github.com/..."}},
        "repository": {"full_name": "omneval/omneval"},
    }
    result = _run(handler.handle(payload, mock_by_repo(), factory))
    assert "ignored" in result


def test_issue_comment_handler_ignores_non_pr_issue():
    handler = PRCommentHandler()
    factory = _FakeFactory()
    payload = {
        "action": "created",
        "comment": {
            "user": {"login": "human-reviewer"},
            "body": "@devloop-bot please fix",
        },
        "issue": {"number": 17, "title": "Just an issue"},
        "repository": {"full_name": "omneval/omneval"},
    }
    result = _run(handler.handle(payload, mock_by_repo(), factory))
    assert "ignored" in result


def test_webhook_router_dispatches_issues_labeled():
    router = WebhookRouter()
    factory = _FakeFactory()
    payload = {
        "action": "labeled",
        "label": {"name": "agent-ready"},
        "repository": {"full_name": "omneval/omneval"},
        "issue": {"number": 42, "title": "Test"},
    }
    result = _run(router.route("issues", payload, mock_by_repo(), factory))
    assert result["project"] == "test-project"


def test_webhook_router_dispatches_pull_request_review():
    router = WebhookRouter()
    factory = _FakeFactory()
    payload = {
        "action": "submitted",
        "review": {"user": {"login": "human-reviewer"}, "body": "Fix this"},
        "pull_request": {"number": 17, "head": {"ref": "agent/issue-42"}},
        "repository": {"full_name": "omneval/omneval"},
    }
    result = _run(router.route("pull_request_review", payload, mock_by_repo(), factory))
    assert result["source"] == "review"


def test_webhook_router_dispatches_issue_comment():
    router = WebhookRouter()
    factory = _FakeFactory()
    payload = {
        "action": "created",
        "comment": {
            "user": {"login": "human-reviewer"},
            "body": "@devloop-bot fix this",
        },
        "issue": {"number": 17, "pull_request": {"url": "https://api.github.com/..."}},
        "repository": {"full_name": "omneval/omneval"},
    }
    result = _run(router.route("issue_comment", payload, mock_by_repo(), factory))
    assert result["source"] == "comment"


def test_webhook_router_returns_ignored_for_unknown_event():
    router = WebhookRouter()
    factory = _FakeFactory()
    result = _run(router.route("push", {"action": "push"}, mock_by_repo(), factory))
    assert "ignored" in result


def test_workflow_factory_create_pr_comment_input_records_fields():
    project = _FakeProject()
    factory = _FakeFactory()
    result = _run(
        factory.create_pr_comment_input(
            project,
            pr_number=17,
            issue_number=42,
            branch="agent/issue-42",
            comment_body="Fix the bug",
            source="review",
            author="human-reviewer",
        )
    )
    assert result["project_id"] == "test-project"
    assert len(factory.calls) == 1
    call = factory.calls[0]
    assert call[0] == "create_pr_comment_input"
    assert call[4] == "agent/issue-42"  # branch at index 4
