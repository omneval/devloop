"""I/O activities for the Summarization workflow (issue #24).

* dedup state (last-summarized commit SHA per project) is kept in a ConfigMap.
* the change set is read from the GitHub compare API (no clone needed).
* the digest is produced by a single-turn LLM call against the homelab model.
"""

from __future__ import annotations

import json
import logging
import os

from temporalio import activity

from .github_ops import _client  # reuse the authed httpx client
from .projects import get_project, parse_github_repo
from .summarization import SummarizeInput, SummarizeResult

log = logging.getLogger(__name__)

NAMESPACE = os.getenv("AGENTS_NAMESPACE", "agents")
STATE_CONFIGMAP = os.getenv("SUMMARY_STATE_CONFIGMAP", "dev-loop-summary-state")
OPENAI_BASE_URL = os.getenv("AGENT_OPENAI_BASE_URL", "http://192.168.68.104/v1")
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "qwen3-27b")


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
# kubernetes ConfigMap state (patched in tests)
# --------------------------------------------------------------------------- #
def _core():
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CoreV1Api()


def get_last_sha(project_id: str) -> str:
    from kubernetes.client.exceptions import ApiException

    try:
        cm = _core().read_namespaced_config_map(STATE_CONFIGMAP, NAMESPACE)
        data = (cm.data or {}) if not isinstance(cm, dict) else cm.get("data", {})
        return json.loads(data.get("last-sha", "{}")).get(project_id, "")
    except ApiException as exc:
        if getattr(exc, "status", None) == 404:
            return ""
        raise


def set_last_sha(project_id: str, sha: str) -> None:
    core = _core()
    cm = core.read_namespaced_config_map(STATE_CONFIGMAP, NAMESPACE)
    data = (cm.data or {}) if not isinstance(cm, dict) else cm.get("data", {})
    mapping = json.loads(data.get("last-sha", "{}"))
    mapping[project_id] = sha
    core.patch_namespaced_config_map(
        STATE_CONFIGMAP, NAMESPACE, {"data": {"last-sha": json.dumps(mapping)}}
    )


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

    resp = httpx.post(
        f"{OPENAI_BASE_URL}/chat/completions",
        json={
            "model": SUMMARY_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


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
