"""GitHub REST activities for the Dev Loop (issues #20, #22, #23).

Network access is via ``httpx`` against the GitHub REST API. Each enrolled
project carries its own scoped GitHub token (``github_token_secret`` in the
registry); the token is resolved per project from that Secret at call time, so
different orgs/owners use different credentials. The pure planning logic
(``build_plan``) is separated from the HTTP fetch so it can be unit-tested
without a network.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from temporalio import activity

from .projects import ProjectConfig, get_project, parse_github_repo
from .shared import ExecutionPlan, OpenAgentPRsInput, PlannedIssue

log = logging.getLogger(__name__)

GITHUB_API = os.getenv("GITHUB_API", "https://api.github.com")
NAMESPACE = os.getenv("AGENTS_NAMESPACE", "agents")

# Matches "#123" and "depends on #123" / "blocked by #123" references.
_ISSUE_REF = re.compile(r"#(\d+)")
_DEP_HINT = re.compile(r"(?:depends on|blocked by|after)\s+#(\d+)", re.IGNORECASE)
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
# Pure planning logic
# --------------------------------------------------------------------------- #
def _extract_deps(number: int, body: str, known: set[int]) -> list[int]:
    """Return issue numbers this issue depends on (referenced + in this batch)."""
    deps: set[int] = set()
    for m in _DEP_HINT.finditer(body or ""):
        deps.add(int(m.group(1)))
    # also treat any bare #ref to another in-batch issue as a soft dependency
    for m in _ISSUE_REF.finditer(body or ""):
        ref = int(m.group(1))
        if ref in known:
            deps.add(ref)
    deps.discard(number)
    return sorted(d for d in deps if d in known)


def build_plan(project_id: str, raw_issues: list[dict[str, Any]]) -> ExecutionPlan:
    """Build a dependency-ordered ExecutionPlan from raw GitHub issue dicts.

    Issues that reference other in-batch issues are scheduled after them
    (stable topological sort; cycles fall back to issue-number order).
    """
    issues = [
        PlannedIssue(
            number=i["number"], title=i.get("title", ""), body=i.get("body") or ""
        )
        for i in raw_issues
        if "pull_request" not in i  # exclude PRs (the issues API returns both)
    ]
    known = {i.number for i in issues}
    for issue in issues:
        issue.depends_on = _extract_deps(issue.number, issue.body, known)

    # Stable topological sort by number, honoring depends_on.
    by_number = {i.number: i for i in issues}
    ordered: list[PlannedIssue] = []
    placed: set[int] = set()

    def visit(n: int, stack: set[int]) -> None:
        if n in placed or n not in by_number:
            return
        if n in stack:  # cycle — break it
            return
        stack.add(n)
        for dep in by_number[n].depends_on:
            visit(dep, stack)
        stack.discard(n)
        if n not in placed:
            placed.add(n)
            ordered.append(by_number[n])

    for n in sorted(by_number):
        visit(n, set())

    return ExecutionPlan(project_id=project_id, issues=ordered)


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _read_token(secret_name: str) -> str:
    """Read ``GITHUB_TOKEN`` from the named Secret in the agents namespace."""
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    sec = client.CoreV1Api().read_namespaced_secret(secret_name, NAMESPACE)
    raw = (sec.data or {}).get("GITHUB_TOKEN", "")
    return base64.b64decode(raw).decode() if raw else ""


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _client(cfg: ProjectConfig):
    import httpx

    token = _read_token(cfg.github_token_secret)
    return httpx.Client(base_url=GITHUB_API, headers=_headers(token), timeout=30.0)


# --------------------------------------------------------------------------- #
# Activity inputs
# --------------------------------------------------------------------------- #
@dataclass
class PlanInput:
    project_id: str
    feedback: str = ""  # appended re-plan guidance (unused by the fetch, logged)


@dataclass
class InlineComment:
    file: str
    line: int
    body: str


@dataclass
class PostCommentsInput:
    project_id: str
    pr_number: int
    summary: str
    inline_comments: list[InlineComment]


@dataclass
class NewIssue:
    title: str
    body: str


@dataclass
class FileIssuesInput:
    project_id: str
    issues: list[NewIssue]


@dataclass
class CloseIssuesInput:
    project_id: str
    issue_numbers: list[int]
    comment: str


# --------------------------------------------------------------------------- #
# Activities
# --------------------------------------------------------------------------- #
@activity.defn
async def plan_issues(inp: PlanInput) -> ExecutionPlan:
    cfg = get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)
    if inp.feedback:
        log.info("re-planning %s with feedback: %s", repo, inp.feedback[:200])

    raw: list[dict[str, Any]] = []
    with _client(cfg) as c:
        page = 1
        while True:
            resp = c.get(
                f"/repos/{repo}/issues",
                params={
                    "state": "open",
                    "labels": cfg.agent_label,
                    "per_page": 100,
                    "page": page,
                },
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            raw.extend(batch)
            page += 1

    return build_plan(inp.project_id, raw)


@activity.defn
async def post_pr_comments(inp: PostCommentsInput) -> None:
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


@activity.defn
async def close_issues(inp: CloseIssuesInput) -> None:
    cfg = get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)
    with _client(cfg) as c:
        for number in inp.issue_numbers:
            c.post(
                f"/repos/{repo}/issues/{number}/comments",
                json={"body": inp.comment},
            ).raise_for_status()
            c.patch(
                f"/repos/{repo}/issues/{number}",
                json={"state": "closed", "state_reason": "completed"},
            ).raise_for_status()
    log.info("closed issues in %s: %s", repo, inp.issue_numbers)
