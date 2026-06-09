"""I/O activities for the Summarization workflow (issue #24, #79).

* dedup state (last-summarized commit SHA per project) is kept in a ConfigMap.
* the change set is read from the GitHub compare API (no clone needed).
* the digest is produced by a single-turn LLM call against the homelab model.
* the digest is published as a GitHub Issue with label ``devloop-summary``.
* optionally POSTs the payload to ``SUMMARIZATION_WEBHOOK_URL`` (fire-and-forget).
"""

from __future__ import annotations

import json
import logging
import os

from pydantic import BaseModel
from temporalio import activity

from . import cluster
from .github_ops import _client  # reuse the authed httpx client
from .projects import get_project, parse_github_repo
from .shared import PublishSummaryInput
from .summarization import SummarizeInput, SummarizeResult


class SummaryOutput(BaseModel):
    """Structured output for the weekly digest LLM call."""

    summary: str


log = logging.getLogger(__name__)

STATE_CONFIGMAP = os.getenv("SUMMARY_STATE_CONFIGMAP", "dev-loop-summary-state")
LLM_BASE_URL = os.getenv("AGENT_LLM_BASE_URL", "http://192.168.68.104/v1")
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "qwen3-27b")

SUMMARY_LABEL = "devloop-summary"


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def should_summarize(last_sha: str, head_sha: str, closed_issues: list[int]) -> bool:
    """Skip only when nothing new has landed since the last summary."""
    if closed_issues:
        return True
    if not head_sha:
        return False
    return head_sha != last_sha


def build_prompt(commits: list[str], issues: list[dict]) -> str:
    commit_block = "\n".join(f"- {c}" for c in commits) or "- (no new commits)"
    issue_block = (
        "\n".join(f"- #{i['number']} {i['title']}" for i in issues) or "- (none)"
    )
    return (
        "You are writing a changelog entry for a homelab Kubernetes repo. "
        "Given the commit messages and resolved issues below, write a short "
        "plain-English paragraph explaining WHAT changed and WHY, followed by a "
        "bullet list of the resolved issues by title. Do NOT include raw diff "
        "lines or git hashes.\n\n"
        f"Commit messages:\n{commit_block}\n\nResolved issues:\n{issue_block}\n"
    )


# --------------------------------------------------------------------------- #
# Dedup state — last-summarized SHA per project, kept in a ConfigMap
# --------------------------------------------------------------------------- #
def get_last_sha(project_id: str) -> str:
    data = cluster.read_configmap_data(STATE_CONFIGMAP) or {}
    return json.loads(data.get("last-sha", "{}")).get(project_id, "")


def set_last_sha(project_id: str, sha: str) -> None:
    data = cluster.read_configmap_data(STATE_CONFIGMAP) or {}
    mapping = json.loads(data.get("last-sha", "{}"))
    mapping[project_id] = sha
    cluster.patch_configmap_data(STATE_CONFIGMAP, {"last-sha": json.dumps(mapping)})


# --------------------------------------------------------------------------- #
# GitHub + LLM
# --------------------------------------------------------------------------- #
def _fetch_changes(
    repo: str, base: str, head: str, closed: list[int]
) -> tuple[list[str], list[dict], str]:
    commits: list[str] = []
    issues: list[dict] = []
    resolved_head = head
    with _client() as c:
        if not resolved_head:
            r = c.get(f"/repos/{repo}/commits", params={"per_page": 1})
            r.raise_for_status()
            resolved_head = r.json()[0]["sha"]
        if base and base != resolved_head:
            r = c.get(f"/repos/{repo}/compare/{base}...{resolved_head}")
            if r.status_code == 200:
                commits = [
                    cm["commit"]["message"].splitlines()[0]
                    for cm in r.json().get("commits", [])
                ]
        for n in closed:
            r = c.get(f"/repos/{repo}/issues/{n}")
            if r.status_code == 200:
                j = r.json()
                issues.append({"number": j["number"], "title": j["title"]})
    return commits, issues, resolved_head


def _llm_summary(prompt: str) -> str:
    import httpx

    schema = SummaryOutput.model_json_schema()
    resp = httpx.post(
        f"{LLM_BASE_URL}/chat/completions",
        json={
            "model": SUMMARY_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": SummaryOutput.__name__,
                    "schema": schema,
                },
            },
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"].strip()
    model = SummaryOutput.model_validate_json(raw)
    return model.summary


def _ensure_label(c, repo: str) -> None:
    """Create the ``devloop-summary`` label on *repo* if it does not exist."""
    r = c.get(f"/repos/{repo}/labels/{SUMMARY_LABEL}")
    if r.status_code == 404:
        c.post(
            f"/repos/{repo}/labels",
            json={
                "name": SUMMARY_LABEL,
                "color": "0075ca",
                "description": "Weekly devloop summary",
            },
        ).raise_for_status()
        log.info("created label %r on %s", SUMMARY_LABEL, repo)


# --------------------------------------------------------------------------- #
# Activities
# --------------------------------------------------------------------------- #


@activity.defn
async def summarize_changes(inp: SummarizeInput) -> SummarizeResult:
    cfg = get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)
    last_sha = get_last_sha(inp.project_id)

    commits, issues, head = _fetch_changes(
        repo, last_sha, inp.head_sha, inp.closed_issues
    )

    if not should_summarize(last_sha, head, inp.closed_issues):
        return SummarizeResult(skipped=True, head_sha=head)

    summary = _llm_summary(build_prompt(commits, issues))
    set_last_sha(inp.project_id, head)
    return SummarizeResult(skipped=False, summary=summary, head_sha=head)


@activity.defn
async def publish_summary(inp: PublishSummaryInput) -> None:
    """Publish a weekly digest as a GitHub Issue on the enrolled repo.

    1. Ensures the ``devloop-summary`` label exists (creates it if absent).
    2. Opens a GitHub Issue titled ``[devloop] {project_id} — {date} digest``
       with the summary as the body and the ``devloop-summary`` label.
    3. If ``SUMMARIZATION_WEBHOOK_URL`` is set and non-empty, fires a POST with
       ``{"project_id": ..., "summary": ..., "date": ...}`` as JSON.
       Webhook failure is logged but does NOT fail the activity (fire-and-forget).
    """
    # Accept both dataclass and plain dict (the workflow passes a dict via args=[]).
    if isinstance(inp, dict):
        inp = PublishSummaryInput(**inp)

    cfg = get_project(inp.project_id)
    repo = parse_github_repo(cfg.github_url)

    title = f"[devloop] {inp.project_id} — {inp.date} digest"

    with _client(cfg) as c:
        _ensure_label(c, repo)
        resp = c.post(
            f"/repos/{repo}/issues",
            json={
                "title": title,
                "body": inp.summary,
                "labels": [SUMMARY_LABEL],
            },
        )
        resp.raise_for_status()
        issue_number = resp.json().get("number")
        log.info(
            "created GitHub Issue #%s '%s' on %s",
            issue_number,
            title,
            repo,
        )

    # Optional outbound webhook (fire-and-forget).
    webhook_url = os.getenv("SUMMARIZATION_WEBHOOK_URL", "").strip()
    if webhook_url:
        import httpx

        try:
            httpx.post(
                webhook_url,
                json={
                    "project_id": inp.project_id,
                    "summary": inp.summary,
                    "date": inp.date,
                },
                timeout=10.0,
            )
            log.info("posted summary webhook to %s", webhook_url)
        except Exception:
            log.warning(
                "webhook delivery to %s failed (fire-and-forget; workflow not failed)",
                webhook_url,
                exc_info=True,
            )
