"""Restart stuck Dev Loop workflows.

When a Dev Loop workflow completes or fails while open `agent-ready` issues
remain in the repository, those issues are silently skipped until a new webhook
trigger is received. Use this script to send a fresh trigger to the Temporal
Orchestration Worker webhook so the DevLoopWorkflow resumes for each affected
project.

Because the webhook uses WorkflowIDConflictPolicy.USE_EXISTING, posting once
per project is safe: running workflows are not disturbed. When a workflow is
completed or absent, a new run starts and self-discovers all open agent-ready
issues.

Usage
-----
Port-forward the webhook endpoint, then run:

    kubectl port-forward -n <namespace> svc/devloop-temporal-worker 8088:8088 &

    uv run scripts/restart_workflows.py \\
      --webhook-url http://localhost:8088/webhook/github \\
      --repo owner/project \\
      --repo owner/other-project

Environment variables
---------------------
GITHUB_TOKEN          : GitHub PAT with repo:read scope (also via --github-token).
GITHUB_WEBHOOK_SECRET : Webhook HMAC secret for payload signing (also via
                        --webhook-secret). Only needed when the worker was
                        deployed with GITHUB_WEBHOOK_SECRET set.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import sys
from dataclasses import dataclass

import httpx

GITHUB_API = "https://api.github.com"

log = logging.getLogger("restart_workflows")


@dataclass
class OpenIssuesResult:
    repo: str
    numbers: list[int]

    @property
    def count(self) -> int:
        return len(self.numbers)


@dataclass
class TriggerResult:
    repo: str
    open_issues: int  # -1 when the GitHub check was skipped
    workflow_id: str | None
    status_code: int | None
    skipped: bool = False
    error: str | None = None

    @property
    def success(self) -> bool:
        return (
            not self.skipped
            and self.error is None
            and self.status_code is not None
            and self.status_code < 300
        )


class IssueChecker:
    """Fetches open issues with a given label from the GitHub API."""

    def __init__(self, client: httpx.Client):
        self._client = client

    def fetch_open(self, repo: str, label: str, github_token: str) -> OpenIssuesResult:
        """Return open issues in *repo* carrying *label*."""
        resp = self._client.get(
            f"{GITHUB_API}/repos/{repo}/issues",
            params={"state": "open", "labels": label, "per_page": 100},
            headers={
                "Authorization": f"token {github_token}",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        resp.raise_for_status()
        issues = resp.json()
        return OpenIssuesResult(repo=repo, numbers=[i["number"] for i in issues])


class WebhookPoster:
    """Posts a GitHub labeled-issue event to the Temporal webhook."""

    def __init__(self, client: httpx.Client):
        self._client = client

    def post(
        self,
        webhook_url: str,
        repo: str,
        label: str,
        webhook_secret: str = "",
    ) -> tuple[int, dict]:
        """POST a minimal issues/labeled payload to *webhook_url*.

        Signs the body with HMAC-SHA256 when *webhook_secret* is non-empty.
        Returns (status_code, response_json).
        """
        payload = {
            "action": "labeled",
            "label": {"name": label},
            "repository": {"full_name": repo},
            "issue": {"number": 0, "title": "restart trigger"},
        }
        body = json.dumps(payload).encode()
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-GitHub-Event": "issues",
        }
        if webhook_secret:
            mac = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
            headers["X-Hub-Signature-256"] = f"sha256={mac}"

        resp = self._client.post(webhook_url, content=body, headers=headers)
        try:
            body_json: dict = resp.json()
        except Exception:
            body_json = {}
        return resp.status_code, body_json


def restart_project(
    checker: IssueChecker,
    poster: WebhookPoster,
    webhook_url: str,
    repo: str,
    label: str,
    github_token: str | None,
    webhook_secret: str,
    dry_run: bool,
) -> TriggerResult:
    """Trigger (or report) a workflow restart for one project repo."""
    open_count = -1

    if github_token:
        try:
            issues = checker.fetch_open(repo, label, github_token)
            open_count = issues.count
            if issues.count == 0:
                log.info("%s: no open '%s' issues — skipping", repo, label)
                return TriggerResult(
                    repo=repo,
                    open_issues=0,
                    workflow_id=None,
                    status_code=None,
                    skipped=True,
                )
            log.info("%s: %d open issue(s): %s", repo, issues.count, issues.numbers)
        except httpx.HTTPError as exc:
            log.warning(
                "%s: could not fetch issues (%s) — triggering anyway", repo, exc
            )

    # Workflow IDs follow devloop-<project_id>; project_id matches the repo name
    # component in a standard deployment, but the authoritative value is returned
    # in the webhook response body.
    wf_id = f"devloop-{repo.split('/')[-1]}"

    if dry_run:
        issues_info = f" ({open_count} open issue(s))" if open_count >= 0 else ""
        log.info("%s: [dry-run] would trigger %s%s", repo, wf_id, issues_info)
        return TriggerResult(
            repo=repo,
            open_issues=open_count,
            workflow_id=wf_id,
            status_code=None,
            skipped=True,
        )

    try:
        status, body = poster.post(webhook_url, repo, label, webhook_secret)
    except httpx.HTTPError as exc:
        return TriggerResult(
            repo=repo,
            open_issues=open_count,
            workflow_id=wf_id,
            status_code=None,
            error=str(exc),
        )

    returned_wf_id = body.get("workflow_id", wf_id)
    if status < 300:
        log.info("%s: triggered %s (HTTP %d)", repo, returned_wf_id, status)
    else:
        log.warning("%s: webhook returned %d: %s", repo, status, body)

    return TriggerResult(
        repo=repo,
        open_issues=open_count,
        workflow_id=returned_wf_id,
        status_code=status,
        error=None if status < 300 else f"HTTP {status}",
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--webhook-url",
        required=True,
        help="Temporal worker webhook URL, e.g. http://localhost:8088/webhook/github",
    )
    parser.add_argument(
        "--repo",
        action="append",
        required=True,
        dest="repos",
        metavar="OWNER/NAME",
        help="GitHub repository in owner/name format; repeat for multiple repos",
    )
    parser.add_argument(
        "--label",
        default="agent-ready",
        help="Agent trigger label (default: agent-ready)",
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get("GITHUB_TOKEN"),
        help=(
            "GitHub PAT with repo:read scope "
            "(falls back to GITHUB_TOKEN env var). "
            "Used to check for open issues before triggering."
        ),
    )
    parser.add_argument(
        "--webhook-secret",
        default=os.environ.get("GITHUB_WEBHOOK_SECRET", ""),
        help=(
            "Webhook HMAC secret for payload signing "
            "(falls back to GITHUB_WEBHOOK_SECRET env var). "
            "Required only when the worker was deployed with GITHUB_WEBHOOK_SECRET set."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be triggered without sending any requests",
    )
    args = parser.parse_args()

    if not args.github_token:
        log.warning(
            "No GitHub token — skipping open-issue count. "
            "Set GITHUB_TOKEN or pass --github-token to enable pre-flight checks."
        )

    results: list[TriggerResult] = []
    with httpx.Client(timeout=30.0) as client:
        checker = IssueChecker(client)
        poster = WebhookPoster(client)
        for repo in args.repos:
            result = restart_project(
                checker,
                poster,
                args.webhook_url,
                repo,
                args.label,
                args.github_token,
                args.webhook_secret,
                args.dry_run,
            )
            results.append(result)

    triggered = [r for r in results if r.success]
    skipped = [r for r in results if r.skipped]
    failed = [r for r in results if not r.skipped and not r.success]

    print(f"\n{len(triggered)} triggered, {len(skipped)} skipped, {len(failed)} failed")
    for r in triggered:
        issues_info = f" ({r.open_issues} open issue(s))" if r.open_issues >= 0 else ""
        print(f"  OK  {r.repo} -> {r.workflow_id}{issues_info}")
    for r in skipped:
        reason = "dry-run" if args.dry_run else "no open issues"
        print(f"  --  {r.repo} ({reason})")
    for r in failed:
        print(f"  !! {r.repo}: {r.error}")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
