"""GitHub issue poller – lightweight alternative to a direct webhook.

Periodically queries the GitHub Issues API for issues labelled with the
project ``agent_label`` (e.g. ``agent-ready``) and forwards any *new* ones
to the Temporal worker's ``POST /webhook/github`` endpoint.

State (already-seen issue numbers) is persisted to a JSON file on a mounted
PVC so that restarts do not re-fire workflows.

Environment variables
---------------------
GITHUB_TOKEN          : Personal access token (or fine-grained PAT) with repo
                        read scope on the target organisation.
WEBHOOK_URL           : Full URL of the temporal-worker webhook endpoint,
                        e.g. ``http://temporal-worker.agents.svc.cluster.local:8088/webhook/github``
GITHUB_REPO           : Full GitHub repo name, e.g. ``omneval/omneval``
AGENT_LABEL           : The label that triggers a workflow, e.g. ``agent-ready``
POLL_INTERVAL_SECONDS : Seconds between API calls (default 300 / 5 min).
STATE_FILE            : Path to the JSON file tracking processed issues
                        (default ``/data/state.json``).
HEALTH_PORT           : Port for the ``/healthz`` probe (default ``8080``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("poller")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
GITHUB_REPO = os.environ["GITHUB_REPO"]
AGENT_LABEL = os.environ.get("AGENT_LABEL", "agent-ready")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8080"))

GITHUB_API = "https://api.github.com"

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def load_state() -> set[int]:
    """Return the set of issue numbers already forwarded."""
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except (json.JSONDecodeError, TypeError):
            log.warning("Corrupt state file %s – starting fresh", STATE_FILE)
    return set()


def save_state(seen: set[int]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(seen)))


# ---------------------------------------------------------------------------
# Health server (for K8s probes)
# ---------------------------------------------------------------------------


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):  # noqa: A002
        pass


def _start_health_server() -> None:
    HTTPServer(("", HEALTH_PORT), _HealthHandler).serve_forever()


# ---------------------------------------------------------------------------
# GitHub polling
# ---------------------------------------------------------------------------


async def fetch_labeled_issues(client: httpx.AsyncClient) -> list[dict]:
    """Return issues currently carrying *AGENT_LABEL* on *GITHUB_REPO*."""
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/issues"
    params = {
        "state": "all",
        "labels": AGENT_LABEL,
        "per_page": 100,
    }
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    result: list[dict] = []
    page = 1
    while True:
        resp = await client.get(url, params={**params, "page": page}, headers=headers)
        resp.raise_for_status()
        issues = resp.json()
        if not issues:
            break
        result.extend(issues)
        page += 1
        if len(issues) < 100:
            break
    return result


async def forward_to_webhook(client: httpx.AsyncClient, issue: dict) -> bool:
    """POST a minimal GitHub ``issues/labeled`` payload to the webhook.

    Returns ``True`` only when the webhook accepts the issue (2xx). On any
    failure (non-2xx response or transport error) returns ``False`` so the
    caller can leave the issue out of ``seen`` and retry on the next cycle.
    """
    payload = {
        "action": "labeled",
        "label": {"name": AGENT_LABEL},
        "repository": {"full_name": GITHUB_REPO},
        "issue": {
            "number": issue["number"],
            "title": issue.get("title", ""),
            "state": issue.get("state", "open"),
        },
    }
    try:
        resp = await client.post(
            WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json", "X-GitHub-Event": "issues"},
        )
    except httpx.HTTPError as exc:
        log.warning("webhook request failed for issue #%d: %s", issue["number"], exc)
        return False

    if resp.status_code < 300:
        log.info(
            "forwarded issue #%d → %s (status %d)",
            issue["number"],
            WEBHOOK_URL,
            resp.status_code,
        )
        return True

    log.warning(
        "webhook returned %d for issue #%d: %s",
        resp.status_code,
        issue["number"],
        resp.text[:200],
    )
    return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def poll_once(seen: set[int]) -> set[int]:
    """Run a single poll cycle. Returns updated ``seen`` set."""
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        issues = await fetch_labeled_issues(client)

        new_issues = [i for i in issues if i["number"] not in seen]
        if not new_issues:
            log.debug("no new issues on this cycle")
            return seen

        log.info("found %d new issue(s) with label '%s'", len(new_issues), AGENT_LABEL)

        forwarded_any = False
        for issue in new_issues:
            if await forward_to_webhook(client, issue):
                seen.add(issue["number"])
                forwarded_any = True

        # Only persist when something was actually accepted; a failed forward
        # is left out of ``seen`` so it retries on the next cycle.
        if forwarded_any:
            save_state(seen)
    return seen


async def main() -> None:
    threading.Thread(target=_start_health_server, daemon=True).start()

    seen = load_state()
    log.info(
        "Polling %s for label '%s' every %ds → %s (state: %s, already seen: %d)",
        GITHUB_REPO,
        AGENT_LABEL,
        POLL_INTERVAL,
        WEBHOOK_URL,
        STATE_FILE,
        len(seen),
    )

    while True:
        try:
            seen = await poll_once(seen)
        except Exception:
            log.exception("poll cycle failed")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
