"""GitHub REST activities for the Dev Loop (issues #22, #23).

Network access is via ``httpx`` against the GitHub REST API. Each enrolled
project carries its own scoped GitHub token (``github_token_secret`` in the
registry); the token is resolved per project from that Secret at call time, so
different orgs/owners use different credentials.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from temporalio import activity

from . import cluster
from .projects import ProjectConfig, get_project, parse_github_repo
from .shared import (
    CICheckFailure,
    CIChecksResult,
    GetPRDiffInput,
    GithubNotificationInput,
    OpenAgentPRsInput,
    PollCIChecksInput,
    PostCommentsInput,
    RequestReviewerInput,
)

log = logging.getLogger(__name__)

GITHUB_API = os.getenv("GITHUB_API", "https://api.github.com")

# Agent issue branches are named ``agent/issue-<N>[-slug]`` (see entrypoint.py).
_AGENT_BRANCH = re.compile(r"^agent/issue-(\d+)")


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


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _client(cfg: ProjectConfig):
    import httpx

    token = cluster.read_secret_value(cfg.github_token_secret, "GITHUB_TOKEN")
    return httpx.Client(base_url=GITHUB_API, headers=_headers(token), timeout=30.0)


# --------------------------------------------------------------------------- #
# Activity inputs
# --------------------------------------------------------------------------- #
@dataclass
class NewIssue:
    title: str
    body: str


@dataclass
class FileIssuesInput:
    project_id: str
    issues: list[NewIssue] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Activities
# --------------------------------------------------------------------------- #
@activity.defn
async def post_pr_comments(inp: PostCommentsInput) -> None:
    """Post the reviewer's findings to a PR: a summary comment plus any
    line-anchored inline review comments. Called by the Dev Loop after the review
    Agent Execution Job returns its ``review`` payload.

    Raises ``ValueError`` when the input is genuinely invalid (empty summary
    with no inline comments, or unresolvable PR number) so failures surface
    as errors rather than silent no-ops.
    """
    if not inp.summary and not inp.inline_comments:
        raise ValueError(
            "post_pr_comments requires a non-empty summary or inline comments"
        )
    if inp.pr_number <= 0:
        raise ValueError("post_pr_comments requires a valid pr_number (got 0)")

    cfg = get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)
    with _client(cfg) as c:
        # PR-level summary comment
        c.post(
            f"/repos/{repo}/issues/{inp.pr_number}/comments",
            json={"body": f"### Agent review\n\n{inp.summary}"},
        ).raise_for_status()
        # Inline review comments (best-effort; needs the head commit SHA)
        if inp.inline_comments:
            pr = c.get(f"/repos/{repo}/pulls/{inp.pr_number}")
            pr.raise_for_status()
            commit_id = pr.json()["head"]["sha"]
            comments = [
                {"path": ic.file, "line": ic.line, "side": "RIGHT", "body": ic.body}
                for ic in inp.inline_comments
            ]
            c.post(
                f"/repos/{repo}/pulls/{inp.pr_number}/reviews",
                json={"commit_id": commit_id, "event": "COMMENT", "comments": comments},
            ).raise_for_status()
    log.info(
        "posted %d inline comment(s) to %s#%d",
        len(inp.inline_comments),
        repo,
        inp.pr_number,
    )


@activity.defn
async def file_issues(inp: FileIssuesInput) -> list[int]:
    """File new agent-ready issues. The seam for the forthcoming QA Validator
    agent, which files follow-up issues for problems it finds; not yet wired into
    any workflow."""
    cfg = get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)
    created: list[int] = []
    with _client(cfg) as c:
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
    """Post a comment on a GitHub Issue using the project's scoped token.

    Used by DevLoopWorkflow to replace all Discord _say/_notify calls with
    in-GitHub Issue comments visible to the project operators. The token is
    resolved per project from the project's ``github_token_secret`` Secret,
    so different orgs/owners use different credentials (same pattern as
    ``post_pr_comments``).
    """
    cfg = get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)
    with _client(cfg) as c:
        c.post(
            f"/repos/{repo}/issues/{inp.issue_number}/comments",
            json={"body": inp.body},
        ).raise_for_status()
    log.info(
        "posted GitHub comment on %s#%d",
        repo,
        inp.issue_number,
    )


@activity.defn
async def request_github_reviewer(inp: RequestReviewerInput) -> None:
    """Request the project's configured ``pr_reviewer`` as a reviewer on a PR.

    Called by DevLoopWorkflow after the review phase. The reviewer is resolved
    from the project registry (``ProjectConfig.pr_reviewer``); the ``reviewer``
    field on the input is used if non-empty, otherwise falls back to the
    project's configured reviewer. A missing or empty reviewer is silently
    skipped (not every project configures one).
    """
    cfg = get_project(inp.project_id)
    reviewer = inp.reviewer or cfg.pr_reviewer
    if not reviewer:
        log.info("no pr_reviewer configured for project %s — skipping", inp.project_id)
        return
    if inp.pr_number <= 0:
        log.info(
            "request_github_reviewer: invalid pr_number %d for project %s — skipping",
            inp.pr_number,
            inp.project_id,
        )
        return
    repo = parse_github_repo(cfg.github_url)
    with _client(cfg) as c:
        c.post(
            f"/repos/{repo}/pulls/{inp.pr_number}/requested_reviewers",
            json={"reviewers": [reviewer]},
        ).raise_for_status()
    log.info(
        "requested %s as reviewer on %s#%d",
        reviewer,
        repo,
        inp.pr_number,
    )


@activity.defn
async def get_pr_diff(inp: GetPRDiffInput) -> str:
    """Fetch the unified diff for a PR via the GitHub REST API.

    Used by ``PRCommentWorkflow`` (#78) to hand the ``Phase.PR_COMMENT`` Agent
    Execution Job the actual code under discussion (alongside the
    reviewer/commenter's feedback) in ``TaskSpec.extra["pr_diff"]`` — so the
    agent can ground its targeted changes in exactly what the human is
    responding to. Returns ``""`` for an unresolvable PR number rather than
    raising, so a transient diff-fetch hiccup doesn't sink the whole workflow
    (the agent still has the comment/review body and branch access to work
    from).
    """
    if inp.pr_number <= 0:
        return ""
    cfg = get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)
    headers = dict(_headers(cluster.read_secret_value(cfg.github_token_secret, "GITHUB_TOKEN")))
    headers["Accept"] = "application/vnd.github.v3.diff"
    import httpx

    with httpx.Client(base_url=GITHUB_API, headers=headers, timeout=30.0) as c:
        resp = c.get(f"/repos/{repo}/pulls/{inp.pr_number}")
        resp.raise_for_status()
        return resp.text


_TERMINAL_CONCLUSIONS = {
    "success",
    "neutral",
    "skipped",
}


@activity.defn
async def poll_ci_checks(inp: PollCIChecksInput) -> CIChecksResult:
    """Poll the GitHub Checks API for the PR's head commit (``gh pr checks``
    equivalent) and report whether every check run has passed.

    Used by ``Phase.CI_FIX`` (DevLoopWorkflow._ci_fix_loop) to decide whether to
    keep retrying and to hand the fix Agent Execution Job the precise set of
    failing checks via ``TaskSpec.extra["ci_check_failures"]``.

    A check run "passes" when its ``status`` is ``completed`` and its
    ``conclusion`` is one of success/neutral/skipped. Anything still in
    progress or queued is treated as not-yet-passed (so the loop waits rather
    than dispatching a fix for a check that simply hasn't finished).
    """
    if inp.pr_number <= 0:
        return CIChecksResult(all_passed=False, failures=[])

    cfg = get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)
    with _client(cfg) as c:
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
    return CIChecksResult(all_passed=all_passed, failures=failures)


@activity.defn
async def open_agent_pr_issue_numbers(inp: OpenAgentPRsInput) -> list[int]:
    """Return issue numbers that already have an open agent PR (head branch
    ``agent/issue-<N>``). The Dev Loop planner uses this to drop issues whose
    work is awaiting human review on a PR, so they aren't re-planned each round.
    """
    cfg = get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)
    pulls: list[dict[str, Any]] = []
    with _client(cfg) as c:
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
