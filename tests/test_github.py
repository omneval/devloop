"""Tests for devloop.github — GitHub I/O types.

Covers InlineComment, PostCommentsInput, GithubNotificationInput,
RequestReviewerInput, ReviewerRequestResult, GetPRBranchInput,
GetPRDiffInput, CreateGithubIssueInput, UpdateGithubIssueInput,
PublishSummaryInput, and PlanIssueInput.
"""

from __future__ import annotations

import dataclasses


class TestInlineComment:
    """InlineComment dataclass lives in devloop.github."""

    def test_importable_from_github_module(self) -> None:
        from devloop.github import InlineComment

        comment = InlineComment(file="main.py", line=10, body="Fix this")
        assert comment.file == "main.py"
        assert comment.line == 10
        assert comment.body == "Fix this"

    def test_dataclass_serialization(self) -> None:
        from devloop.github import InlineComment

        comment = InlineComment(file="app.py", line=42, body="Bug here")
        d = dataclasses_asdict(comment)
        assert d == {"file": "app.py", "line": 42, "body": "Bug here"}


class TestPostCommentsInput:
    """PostCommentsInput dataclass lives in devloop.github."""

    def test_importable_from_github_module(self) -> None:
        from devloop.github import InlineComment, PostCommentsInput

        inp = PostCommentsInput(
            project_id="repo",
            pr_number=42,
            summary="Overall feedback",
            inline_comments=[InlineComment(file="main.py", line=10, body="Fix this")],
        )
        assert inp.project_id == "repo"
        assert inp.pr_number == 42
        assert inp.summary == "Overall feedback"
        assert len(inp.inline_comments) == 1

    def test_empty_inline_comments(self) -> None:
        from devloop.github import PostCommentsInput

        inp = PostCommentsInput(project_id="repo", pr_number=42, summary="OK")
        assert inp.inline_comments == []

    def test_dataclass_serialization(self) -> None:
        from devloop.github import PostCommentsInput

        inp = PostCommentsInput(project_id="repo", pr_number=42, summary="s")
        d = dataclasses_asdict(inp)
        assert d["project_id"] == "repo"
        assert d["pr_number"] == 42
        assert d["summary"] == "s"
        assert "inline_comments" in d


class TestGithubNotificationInput:
    """GithubNotificationInput dataclass lives in devloop.github."""

    def test_importable_from_github_module(self) -> None:
        from devloop.github import GithubNotificationInput

        inp = GithubNotificationInput(issue_number=42, project_id="repo", body="Hello")
        assert inp.issue_number == 42
        assert inp.project_id == "repo"
        assert inp.body == "Hello"


class TestRequestReviewerInput:
    """RequestReviewerInput dataclass lives in devloop.github."""

    def test_importable_from_github_module(self) -> None:
        from devloop.github import RequestReviewerInput

        inp = RequestReviewerInput(project_id="repo", pr_number=42, reviewer="alice")
        assert inp.project_id == "repo"
        assert inp.pr_number == 42
        assert inp.reviewer == "alice"


class TestReviewerRequestResult:
    """ReviewerRequestResult dataclass lives in devloop.github."""

    def test_importable_from_github_module(self) -> None:
        from devloop.github import ReviewerRequestResult

        result = ReviewerRequestResult()
        assert result.requested is False
        assert result.reason == ""

    def test_successful_request(self) -> None:
        from devloop.github import ReviewerRequestResult

        result = ReviewerRequestResult(requested=True)
        assert result.requested is True
        assert result.reason == ""

    def test_failed_request_with_reason(self) -> None:
        from devloop.github import ReviewerRequestResult

        result = ReviewerRequestResult(requested=False, reason="no reviewer configured")
        assert result.requested is False
        assert result.reason == "no reviewer configured"

    def test_dataclass_serialization(self) -> None:
        from devloop.github import ReviewerRequestResult

        result = ReviewerRequestResult(requested=True, reason="done")
        d = dataclasses_asdict(result)
        assert d == {"requested": True, "reason": "done"}


class TestGetPRBranchInput:
    """GetPRBranchInput dataclass lives in devloop.github."""

    def test_importable_from_github_module(self) -> None:
        from devloop.github import GetPRBranchInput

        inp = GetPRBranchInput(project_id="repo", pr_number=42)
        assert inp.project_id == "repo"
        assert inp.pr_number == 42


class TestGetPRDiffInput:
    """GetPRDiffInput dataclass lives in devloop.github."""

    def test_importable_from_github_module(self) -> None:
        from devloop.github import GetPRDiffInput

        inp = GetPRDiffInput(project_id="repo", pr_number=42)
        assert inp.project_id == "repo"
        assert inp.pr_number == 42


class TestCreateGithubIssueInput:
    """CreateGithubIssueInput dataclass lives in devloop.github."""

    def test_importable_from_github_module(self) -> None:
        from devloop.github import CreateGithubIssueInput

        inp = CreateGithubIssueInput(
            project_id="repo",
            title="Bug report",
            body="Something is broken",
            labels=["bug"],
        )
        assert inp.project_id == "repo"
        assert inp.title == "Bug report"
        assert inp.body == "Something is broken"
        assert inp.labels == ["bug"]

    def test_dataclass_serialization(self) -> None:
        from devloop.github import CreateGithubIssueInput

        inp = CreateGithubIssueInput(
            project_id="repo",
            title="T",
            body="B",
            labels=["L"],
        )
        d = dataclasses_asdict(inp)
        assert d == {
            "project_id": "repo",
            "title": "T",
            "body": "B",
            "labels": ["L"],
        }


class TestUpdateGithubIssueInput:
    """UpdateGithubIssueInput dataclass lives in devloop.github."""

    def test_importable_from_github_module(self) -> None:
        from devloop.github import UpdateGithubIssueInput

        inp = UpdateGithubIssueInput(
            project_id="repo", issue_number=42, body="Updated", state="closed"
        )
        assert inp.project_id == "repo"
        assert inp.issue_number == 42
        assert inp.body == "Updated"
        assert inp.state == "closed"

    def test_defaults(self) -> None:
        from devloop.github import UpdateGithubIssueInput

        inp = UpdateGithubIssueInput(project_id="repo", issue_number=42)
        assert inp.body == ""
        assert inp.state == ""

    def test_dataclass_serialization(self) -> None:
        from devloop.github import UpdateGithubIssueInput

        inp = UpdateGithubIssueInput(project_id="repo", issue_number=42)
        d = dataclasses_asdict(inp)
        assert d == {
            "project_id": "repo",
            "issue_number": 42,
            "body": "",
            "state": "",
        }


class TestPublishSummaryInput:
    """PublishSummaryInput dataclass lives in devloop.github."""

    def test_importable_from_github_module(self) -> None:
        from devloop.github import PublishSummaryInput

        inp = PublishSummaryInput(
            project_id="repo",
            summary="Summary text",
            date="2026-06-06",
        )
        assert inp.project_id == "repo"
        assert inp.summary == "Summary text"
        assert inp.date == "2026-06-06"

    def test_dataclass_serialization(self) -> None:
        from devloop.github import PublishSummaryInput

        inp = PublishSummaryInput(
            project_id="repo",
            summary="S",
            date="2026-06-06",
        )
        d = dataclasses_asdict(inp)
        assert d == {
            "project_id": "repo",
            "summary": "S",
            "date": "2026-06-06",
        }


class TestPlanIssueInput:
    """PlanIssueInput dataclass lives in devloop.github."""

    def test_importable_from_github_module(self) -> None:
        from devloop.github import PlanIssueInput

        inp = PlanIssueInput(project_id="repo", issue_number=42)
        assert inp.project_id == "repo"
        assert inp.issue_number == 42


# Convenience alias so tests can use dataclasses_asdict without repeating the import.
def dataclasses_asdict(obj):
    """Wrapper so tests don't need `from dataclasses import asdict as dataclasses_asdict`."""
    return dataclasses.asdict(obj)
