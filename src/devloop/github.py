"""GitHub I/O types for github_ops and pr_comment modules.

Contains all dataclasses that flow to/from GitHub operations — creating
issues, posting comments, requesting reviewers, and delivering summaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InlineComment:
    file: str
    line: int
    body: str


@dataclass
class PostCommentsInput:
    """Reviewer findings posted to a PR: a PR-level ``summary`` plus optional
    line-anchored ``inline_comments``. Built by the workflow from the review
    Agent Execution Job's ``review`` payload, consumed by the ``post_pr_comments``
    activity."""

    project_id: str
    pr_number: int
    summary: str
    inline_comments: list[InlineComment] = field(default_factory=list)


# ---------------------------------------------------------------------------
# GitHub Issue comment notification activity I/O
# ---------------------------------------------------------------------------


@dataclass
class GithubNotificationInput:
    """Input for the post_github_comment activity.

    Posts a comment to a GitHub Issue using the devloop-bot PAT
    (GITHUB_TOKEN env var / project's github_token_secret).
    """

    issue_number: int
    project_id: str
    body: str


# ---------------------------------------------------------------------------
# Reviewer requests
# ---------------------------------------------------------------------------


@dataclass
class RequestReviewerInput:
    """Input for the request_github_reviewer activity.

    Requests a GitHub user as a reviewer on a PR.
    """

    project_id: str
    pr_number: int
    reviewer: str


@dataclass
class ReviewerRequestResult:
    """Outcome of the request_github_reviewer activity: whether a reviewer
    was actually requested, and — when not — why (no reviewer configured,
    no PR to request on, or a GitHub API failure). Lets callers like
    ``_notify_reviewer`` phrase their notification honestly instead of
    assuming the request succeeded (issue #88)."""

    requested: bool = False
    reason: str = ""


# ---------------------------------------------------------------------------
# PR operations
# ---------------------------------------------------------------------------


@dataclass
class GetPRBranchInput:
    """Input for the get_pr_branch activity.

    Resolves a PR's head branch name from its number — needed when the event
    that triggered ``PRCommentWorkflow`` didn't carry it. ``issue_comment``
    webhook payloads (an ``@devloop-bot`` mention in a PR conversation)
    reference the PR only by number; unlike ``pull_request_review`` payloads,
    they carry no ``pull_request.head.ref`` (issue #101).
    """

    project_id: str
    pr_number: int


@dataclass
class GetPRDiffInput:
    """Input for the get_pr_diff activity.

    Standalone activity kept for consumers that register it directly
    (``devloop.github_ops.get_pr_diff``) — devloop's own ``PRCommentWorkflow``
    no longer calls it (the agent fetches the diff itself via ``gh pr diff``,
    issue #98), but removing the symbol entirely broke downstream workers that
    import it for registration on their own task queues.
    """

    project_id: str
    pr_number: int


# ---------------------------------------------------------------------------
# Summarization delivery activity I/O (issue #79)
# ---------------------------------------------------------------------------


@dataclass
class PublishSummaryInput:
    """Input for the publish_summary activity.

    Carries the rendered digest from SummarizationWorkflow to the activity that
    delivers it: opens a GitHub Issue (label ``devloop-summary``, created if
    absent) and optionally POSTs the same payload to ``SUMMARIZATION_WEBHOOK_URL``
    as JSON (fire-and-forget).
    """

    project_id: str
    summary: str
    date: str  # ISO date string, e.g. "2026-06-06"


# ---------------------------------------------------------------------------
# GitHub Issue CRUD
# ---------------------------------------------------------------------------


@dataclass
class CreateGithubIssueInput:
    """Input for the create_github_issue activity.

    Creates a new GitHub Issue with the given title, body, and labels.
    Returns the created issue number.
    """

    project_id: str
    title: str
    body: str
    labels: list[str]


@dataclass
class UpdateGithubIssueInput:
    """Input for the update_github_issue activity.

    Patches an existing GitHub Issue's body and/or state.
    Only non-empty fields are included in the PATCH payload.
    ``state`` accepts ``"closed"`` or ``""`` (no change).
    """

    project_id: str
    issue_number: int
    body: str = ""
    state: str = ""  # accepts "closed" or "" (no change)


# ---------------------------------------------------------------------------
# Plan issue (issue #120)
# ---------------------------------------------------------------------------


@dataclass
class PlanIssueInput:
    """Input for the plan_issue activity (issue #120).

    Lightweight replacement for the Plan Agent Execution Job on webhook-triggered
    runs: fetches the triggering issue from GitHub, confirms it is open and
    carries the project's agent label, and returns a one-issue plan dict that
    ``_execute_phase`` already consumes.
    """

    project_id: str
    issue_number: int