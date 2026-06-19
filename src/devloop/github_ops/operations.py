"""GitHub REST operations (extracted from github_ops).

All GitHub REST calls live here.  Activity-wrapped functions delegate to
private helpers that accept an optional ``token`` string so callers never
touch the auth module's internal cache or locks directly.

Backward-compatible activity wrappers remain importable from
``devloop.github_ops`` via re-exports in ``__init__.py``.
"""

from __future__ import annotations

import logging
import re

import httpx
from dataclasses import dataclass, field
from typing import Any

from temporalio import activity

from .. import cluster
from ..cichecks import CICheckFailure, CIChecksResult, PollCIChecksInput
from ..execution import OpenAgentPRsInput
from ..github import (
    CreateGithubIssueInput,
    GetPRBranchInput,
    GetPRDiffInput,
    GithubNotificationInput,
    PlanIssueInput,
    PostCommentsInput,
    RequestReviewerInput,
    ReviewerRequestResult,
    UpdateGithubIssueInput,
)
from ..projects import ProjectConfig, parse_github_repo

# Import get_project via a lazy accessor so that monkeypatches to
# ``devloop.github_ops.get_project`` (used by existing tests) actually
# intercept the call inside this module.  Using the module-level name
# directly would cache the original before monkeypatch takes effect.
import devloop.github_ops as _github_ops

log = logging.getLogger(__name__)

# ── Pure helpers ──────────────────────────────────────────────────────────

_AGENT_BRANCH = re.compile(r"^agent/issue-(\d+)")


def make_issue_branch(issue_number: int, title: str) -> str:
    """Generate a branch slug in the ``agent/issue-{id}-{slug}`` convention.

    The slug is the title lower-cased, stripped of non-alphanumeric characters
    (spaces become hyphens, everything else dropped), with leading/trailing
    hyphens removed. If the title is empty the slug portion is omitted entirely.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    if slug:
        return f"agent/issue-{issue_number}-{slug}"
    return f"agent/issue-{issue_number}"


def agent_pr_issue_numbers(pulls: list[dict[str, Any]]) -> list[int]:
    """Issue numbers that already have an open agent PR.

    Pure helper (no network) so it is unit-testable. Reads each PR's head branch
    and matches the ``agent/issue-<N>`` convention the execute phase pushes. Used
    by the Dev Loop planner to skip issues whose work is already up for human
    review — under the PR-review merge model an issue stays *open* until its PR
    is merged, so without this filter the planner would re-surface it every round.
    """
    nums: set[int] = set()
    for pr in pulls:
        ref = (pr.get("head") or {}).get("ref", "")
        m = _AGENT_BRANCH.match(ref)
        if m:
            nums.add(int(m.group(1)))
    return sorted(nums)


# ── Token resolution ────────────────────────────────────────────────────


def _resolve_token(cfg: ProjectConfig, token: str | None = None) -> str:
    """Return the token string to use for ``cfg``.

    When ``token`` is provided (e.g. a test stub or an explicitly passed
    installation token), use it directly.  Otherwise fall back to the
    production resolution logic: GitHub App installation token or project PAT.
    """
    if token:
        return token

    if cfg.github_token_secret:
        return cluster.read_secret_value(cfg.github_token_secret, "GITHUB_TOKEN")
    return ""


# ── HTTP helpers ──────────────────────────────────────────────────────────


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _log_github_api_failure(action: str, exc: Exception) -> None:
    """Log a GitHub API failure without raising (issue #87).

    Used by activities that should degrade gracefully on a transient
    GitHub-side hiccup (expired token, rate limit, missing permission, 404,
    5xx, connection error) rather than sinking the whole DevLoopWorkflow
    round — log-and-degrade rather than raise. Logs the status
    code plus a short excerpt of the response body; never logs headers
    (which carry the bearer token).
    """
    if isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        excerpt = resp.text[:200].replace("\n", " ")
        log.warning(
            "%s: GitHub API returned HTTP %d — %s",
            action,
            resp.status_code,
            excerpt,
        )
    else:
        log.warning("%s: GitHub API request failed — %s", action, exc)


# ── Activity inputs ─────────────────────────────────────────────────────


@dataclass
class NewIssue:
    title: str
    body: str


@dataclass
class FileIssuesInput:
    project_id: str
    issues: list[NewIssue] = field(default_factory=list)


# ── Terminal conclusions (used by poll_ci_checks) ────────────────────────

_TERMINAL_CONCLUSIONS = {
    "success",
    "neutral",
    "skipped",
}


# ── Internal async token resolver ───────────────────────────────────────


async def _async_resolve(cfg: ProjectConfig, token: str | None = None) -> str:  # noqa: ANN201
    """Resolve a token for ``cfg`` by delegating to auth or cluster.

    When ``token`` is provided (e.g. a test stub or an explicitly passed
    installation token), use it directly — no auth or cluster calls are made.
    Otherwise: GitHub App installation token when configured, else reads the
    project's PAT.  In tests the caller can monkeypatch ``auth.get_installation_token``
    or ``cluster.read_secret_value`` to supply a fake token.
    """
    if token:
        return token

    from . import auth

    if auth.github_app_configured():
        return await auth.get_installation_token()
    if not cfg.github_token_secret:
        return ""
    return cluster.read_secret_value(cfg.github_token_secret, "GITHUB_TOKEN")


# ── Activity functions ───────────────────────────────────────────────────


@activity.defn
async def post_pr_comments(inp: PostCommentsInput) -> None:
    """Post the reviewer's findings to a PR: a summary comment plus any
    line-anchored inline review comments."""
    if not inp.summary and not inp.inline_comments:
        raise ValueError(
            "post_pr_comments requires a non-empty summary or inline comments"
        )
    if inp.pr_number <= 0:
        raise ValueError("post_pr_comments requires a valid pr_number (got 0)")

    cfg = _github_ops.get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)

    with await _github_ops._client(cfg) as c:
        c.post(
            f"/repos/{repo}/issues/{inp.pr_number}/comments",
            json={"body": f"### Agent review\n\n{inp.summary}"},
        ).raise_for_status()
    # Inline review comments (best-effort; needs the head commit SHA).
    # Wrapping in try/except so a 422 (or any transient GitHub hiccup)
    # never sinks the whole DevLoopWorkflow round — the summary comment
    # already landed above (issue #162).
    inline_count = len(inp.inline_comments) if inp.inline_comments else 0  # type: ignore[union-attr]
    if inline_count:
        try:
            with await _github_ops._client(cfg) as c:
                pr = c.get(f"/repos/{repo}/pulls/{inp.pr_number}")
                pr.raise_for_status()
                commit_id = pr.json()["head"]["sha"]
                comments = [
                    {
                        "path": ic.file,  # type: ignore[union-attr]
                        "line": ic.line,  # type: ignore[union-attr]
                        "side": "RIGHT",
                        "body": ic.body,  # type: ignore[union-attr]
                    }
                    for ic in inp.inline_comments  # type: ignore[union-attr]
                ]
                c.post(
                    f"/repos/{repo}/pulls/{inp.pr_number}/reviews",
                    json={
                        "commit_id": commit_id,
                        "event": "COMMENT",
                        "comments": comments,
                    },
                ).raise_for_status()
        except Exception:
            log.exception(
                "failed to post inline review comments to %s#%d",
                repo,
                inp.pr_number,
            )
    log.info(
        "posted %d inline comment(s) to %s#%d",
        inline_count,
        repo,
        inp.pr_number,
    )


@activity.defn
async def file_issues(inp: FileIssuesInput) -> list[int]:
    """File new agent-ready issues."""
    cfg = _github_ops.get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)

    created: list[int] = []
    with await _github_ops._client(cfg) as c:
        for issue in inp.issues:
            resp = c.post(
                f"/repos/{repo}/issues",
                json={
                    "title": issue.title,
                    "body": issue.body,
                    "labels": [cfg.agent_label],
                },
            )
            resp.raise_for_status()
            created.append(resp.json()["number"])
    log.info("filed %d new issue(s) in %s: %s", len(created), repo, created)
    return created


@activity.defn
async def post_github_comment(inp: GithubNotificationInput) -> None:
    """Post a comment on a GitHub Issue using the project's scoped token."""

    cfg = _github_ops.get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)

    try:
        with await _github_ops._client(cfg) as c:
            c.post(
                f"/repos/{repo}/issues/{inp.issue_number}/comments",
                json={"body": inp.body},
            ).raise_for_status()
    except httpx.HTTPError as exc:
        _log_github_api_failure(
            f"post_github_comment on {repo}#{inp.issue_number}", exc
        )
        return
    log.info(
        "posted GitHub comment on %s#%d",
        repo,
        inp.issue_number,
    )


@activity.defn
async def request_github_reviewer(inp: RequestReviewerInput) -> ReviewerRequestResult:
    """Request the project's configured ``pr_reviewer`` as a reviewer on a PR."""

    cfg = _github_ops.get_project(inp.project_id)
    reviewer = inp.reviewer or cfg.pr_reviewer
    if not reviewer:
        log.info("no pr_reviewer configured for project %s — skipping", inp.project_id)
        return ReviewerRequestResult(
            requested=False, reason="no reviewer is configured for this project"
        )
    if inp.pr_number <= 0:
        log.info(
            "request_github_reviewer: invalid pr_number %d for project %s — skipping",
            inp.pr_number,
            inp.project_id,
        )
        return ReviewerRequestResult(
            requested=False, reason="no pull request to request a reviewer on"
        )
    repo = parse_github_repo(cfg.github_url)

    try:
        with await _github_ops._client(cfg) as c:
            c.post(
                f"/repos/{repo}/pulls/{inp.pr_number}/requested_reviewers",
                json={"reviewers": [reviewer]},
            ).raise_for_status()
    except httpx.HTTPError as exc:
        _log_github_api_failure(
            f"request_github_reviewer ({reviewer}) on {repo}#{inp.pr_number}", exc
        )
        return ReviewerRequestResult(
            requested=False, reason="GitHub API error requesting a reviewer"
        )
    log.info(
        "requested %s as reviewer on %s#%d",
        reviewer,
        repo,
        inp.pr_number,
    )
    return ReviewerRequestResult(requested=True)


@activity.defn
async def get_pr_diff(inp: GetPRDiffInput) -> str:
    """Fetch the unified diff for a PR via the GitHub REST API."""
    if inp.pr_number <= 0:
        return ""

    cfg = _github_ops.get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)

    try:
        with await _github_ops._client(
            cfg, extra_headers={"Accept": "application/vnd.github.v3.diff"}
        ) as c:
            resp = c.get(f"/repos/{repo}/pulls/{inp.pr_number}")
            resp.raise_for_status()
            return resp.text
    except httpx.HTTPError as exc:
        _log_github_api_failure(f"get_pr_diff on {repo}#{inp.pr_number}", exc)
        return ""


@activity.defn
async def get_pr_branch(inp: GetPRBranchInput) -> str:
    """Resolve a PR's head branch name from its number via the GitHub REST API."""

    cfg = _github_ops.get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)

    try:
        with await _github_ops._client(cfg) as c:
            resp = c.get(f"/repos/{repo}/pulls/{inp.pr_number}")
            resp.raise_for_status()
            return (resp.json().get("head") or {}).get("ref", "")
    except httpx.HTTPError as exc:
        _log_github_api_failure(f"get_pr_branch on {repo}#{inp.pr_number}", exc)
        return ""


@activity.defn
async def poll_ci_checks(inp: PollCIChecksInput) -> CIChecksResult:
    """Poll the GitHub Checks API for the PR's head commit and report results."""

    if inp.pr_number <= 0:
        return CIChecksResult(all_passed=False, pending=False, failures=[])

    cfg = _github_ops.get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)

    try:
        with await _github_ops._client(cfg) as c:
            pr = c.get(f"/repos/{repo}/pulls/{inp.pr_number}")
            pr.raise_for_status()
            sha = pr.json()["head"]["sha"]

            runs: list[dict[str, Any]] = []
            page = 1
            while True:
                resp = c.get(
                    f"/repos/{repo}/commits/{sha}/check-runs",
                    params={"per_page": 100, "page": page},
                )
                resp.raise_for_status()
                batch = resp.json().get("check_runs", [])
                if not batch:
                    break
                runs.extend(batch)
                page += 1
    except httpx.HTTPError as exc:
        _log_github_api_failure(f"poll_ci_checks on {repo}#{inp.pr_number}", exc)
        return CIChecksResult(all_passed=False, pending=True, failures=[])

    failures: list[CICheckFailure] = []
    pending = False
    for run in runs:
        status = run.get("status", "")
        conclusion = run.get("conclusion") or ""
        if status != "completed":
            pending = True
            continue
        if conclusion not in _TERMINAL_CONCLUSIONS:
            output = run.get("output") or {}
            failures.append(
                CICheckFailure(
                    name=run.get("name", ""),
                    conclusion=conclusion,
                    details_url=run.get("details_url", ""),
                    summary=output.get("summary", "") or "",
                )
            )

    all_passed = bool(runs) and not failures and not pending
    log.info(
        "CI checks for %s#%d (%s): %d run(s), %d failing, all_passed=%s",
        repo,
        inp.pr_number,
        sha[:8],
        len(runs),
        len(failures),
        all_passed,
    )
    return CIChecksResult(
        all_passed=all_passed,
        pending=pending and not failures,
        failures=failures,
    )


@activity.defn
async def open_agent_pr_issue_numbers(inp: OpenAgentPRsInput) -> list[int]:
    """Return issue numbers that already have an open agent PR."""

    cfg = _github_ops.get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)

    pulls: list[dict[str, Any]] = []
    with await _github_ops._client(cfg) as c:
        page = 1
        while True:
            resp = c.get(
                f"/repos/{repo}/pulls",
                params={"state": "open", "per_page": 100, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            pulls.extend(batch)
            page += 1
    numbers = agent_pr_issue_numbers(pulls)
    log.info("issues with open agent PRs in %s: %s", repo, numbers)
    return numbers


@activity.defn
async def plan_issue(inp: PlanIssueInput) -> dict:
    """Lightweight replacement for the Plan Agent Execution Job."""

    cfg = _github_ops.get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)
    agent_label = cfg.agent_label
    issue_number = inp.issue_number

    try:
        with await _github_ops._client(cfg) as c:
            resp = c.get(f"/repos/{repo}/issues/{issue_number}")
            resp.raise_for_status()
            issue = resp.json()
    except Exception as exc:
        log.warning(
            "plan_issue: failed to fetch issue %d in %s: %s", issue_number, repo, exc
        )
        return {"issues": []}

    if issue.get("state") != "open":
        log.info("plan_issue: issue %d is closed — skipping", issue_number)
        return {"issues": []}

    label_names = {lb.get("name", "") for lb in issue.get("labels", [])}
    if agent_label not in label_names:
        log.info(
            "plan_issue: issue %d missing agent label %r — skipping",
            issue_number,
            agent_label,
        )
        return {"issues": []}

    title = issue.get("title", "")
    branch = make_issue_branch(issue_number, title)
    return {
        "issues": [
            {
                "id": issue_number,
                "title": title,
                "branch": branch,
            }
        ]
    }


@activity.defn
async def create_github_issue(inp: CreateGithubIssueInput) -> int:
    """Create a new GitHub Issue and return its issue number."""

    cfg = _github_ops.get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)

    try:
        with await _github_ops._client(cfg) as c:
            resp = c.post(
                f"/repos/{repo}/issues",
                json={"title": inp.title, "body": inp.body, "labels": inp.labels},
            )
            resp.raise_for_status()
            number: int = resp.json()["number"]
    except httpx.HTTPError as exc:
        _log_github_api_failure(f"create_github_issue in {repo}", exc)
        return 0
    log.info("created GitHub issue #%d in %s", number, repo)
    return number


@activity.defn
async def update_github_issue(inp: UpdateGithubIssueInput) -> None:
    """Patch an existing GitHub Issue's body and/or state."""

    cfg = _github_ops.get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)
    payload: dict = {}
    if inp.body:
        payload["body"] = inp.body
    if inp.state:
        payload["state"] = inp.state

    try:
        with await _github_ops._client(cfg) as c:
            c.patch(
                f"/repos/{repo}/issues/{inp.issue_number}",
                json=payload,
            ).raise_for_status()
    except httpx.HTTPError as exc:
        _log_github_api_failure(
            f"update_github_issue on {repo}#{inp.issue_number}", exc
        )
        return
    log.info(
        "updated GitHub issue #%d in %s (fields: %s)",
        inp.issue_number,
        repo,
        list(payload.keys()),
    )


def _github_api() -> str:
    """Return the GitHub API base URL."""
    from . import auth

    return auth.GITHUB_API
