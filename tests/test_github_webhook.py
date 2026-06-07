"""Tests for GitHub webhook receiver — HMAC signature verification and
agent-ready label → DevLoopWorkflow routing (issue #31).

Strategy: use FastAPI's TestClient (sync) against a webhook app built with a
mock Temporal client so no real Temporal server is needed.

Conventions:
- GITHUB_WEBHOOK_SECRET is patched at the ``webhook`` module level after import.
- The Temporal client is a simple mock with a recorded ``start_workflow`` spy.
- Raw bytes are constructed manually so we can prove the HMAC is computed over
  the exact raw bytes, not a re-serialised JSON body.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from devloop import webhook  # the module under test

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

_PROJECT_ID = "omneval"
_GITHUB_REPO = "omneval/omneval"
_AGENT_LABEL = "agent-ready"
_SECRET = "super-secret-webhook-token"
_AGENT_LOGIN = "devloop-bot"


class _FakeClient:
    """Minimal Temporal client stub — records calls to start_workflow."""

    def __init__(self):
        self.started: list[dict] = []

    async def start_workflow(self, workflow_name, arg, /, *, id, task_queue, **kwargs):
        self.started.append(
            {
                "workflow": workflow_name,
                "project_id": arg.project_id if hasattr(arg, "project_id") else None,
                "id": id,
                "task_queue": task_queue,
                "id_conflict_policy": kwargs.get("id_conflict_policy"),
            }
        )
        return id


def _make_project():
    from devloop.projects import ProjectConfig

    return ProjectConfig(
        id=_PROJECT_ID,
        github_url=f"https://github.com/{_GITHUB_REPO}",
        default_branch="main",
        agent_image="ghcr.io/example/agent:sha-abc",
        agent_label=_AGENT_LABEL,
        omneval_ingest_secret="omneval-ingest-omneval",
        github_token_secret="omneval-agent-github-token",
    )


def _sign(body: bytes, secret: str) -> str:
    """Compute GitHub-style HMAC-SHA256 signature."""
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _labeled_payload(label: str = _AGENT_LABEL, repo: str = _GITHUB_REPO) -> bytes:
    """Return raw JSON bytes for a GitHub ``issues`` labeled event."""
    payload = {
        "action": "labeled",
        "label": {"name": label},
        "repository": {"full_name": repo},
        "issue": {"number": 42, "title": "Test issue"},
    }
    return json.dumps(payload).encode()


@pytest.fixture
def client_and_spy(monkeypatch):
    """Return (TestClient, fake_temporal_client, patched_webhook_module).

    The GITHUB_WEBHOOK_SECRET module attribute is set to _SECRET.
    A fresh app is built for each test so spies are clean.
    """
    fake = _FakeClient()
    monkeypatch.setattr(webhook, "GITHUB_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setattr(webhook, "AGENT_GITHUB_LOGIN", _AGENT_LOGIN)
    app = webhook.create_app(fake, [_make_project()])
    tc = TestClient(app, raise_server_exceptions=True)
    return tc, fake


@pytest.fixture
def client_no_secret(monkeypatch):
    """App with GITHUB_WEBHOOK_SECRET set to empty string (secret not configured)."""
    fake = _FakeClient()
    monkeypatch.setattr(webhook, "GITHUB_WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook, "AGENT_GITHUB_LOGIN", _AGENT_LOGIN)
    app = webhook.create_app(fake, [_make_project()])
    tc = TestClient(app, raise_server_exceptions=True)
    return tc, fake


# ---------------------------------------------------------------------------
# 1. Valid signature + action=labeled + label agent-ready → workflow started
# ---------------------------------------------------------------------------


def test_valid_signature_starts_devloop_workflow(client_and_spy):
    tc, fake = client_and_spy
    body = _labeled_payload()
    sig = _sign(body, _SECRET)

    resp = tc.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )

    assert resp.status_code == 200
    assert len(fake.started) == 1
    wf = fake.started[0]
    assert wf["workflow"] == "DevLoopWorkflow"
    assert wf["project_id"] == _PROJECT_ID
    # Stable per-project ID + USE_EXISTING so N issues collapse to one Dev Loop
    # run (no duplicate workflows / duplicate Discord threads).
    from temporalio.common import WorkflowIDConflictPolicy

    assert wf["id"] == f"devloop-{_PROJECT_ID}"
    assert wf["id_conflict_policy"] == WorkflowIDConflictPolicy.USE_EXISTING


# ---------------------------------------------------------------------------
# 2. Invalid / forged signature → 401 or 403, workflow NOT started
# ---------------------------------------------------------------------------


def test_forged_signature_rejected(client_and_spy):
    tc, fake = client_and_spy
    body = _labeled_payload()
    bad_sig = _sign(body, "wrong-secret")

    resp = tc.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": bad_sig,
            "Content-Type": "application/json",
        },
    )

    assert resp.status_code in (401, 403)
    assert fake.started == []


# ---------------------------------------------------------------------------
# 3. Missing signature header when secret is set → rejected
# ---------------------------------------------------------------------------


def test_missing_signature_rejected_when_secret_set(client_and_spy):
    tc, fake = client_and_spy
    body = _labeled_payload()

    resp = tc.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "Content-Type": "application/json",
            # no X-Hub-Signature-256 header
        },
    )

    assert resp.status_code in (401, 403)
    assert fake.started == []


# ---------------------------------------------------------------------------
# 4a. Valid sig but action != labeled → 200 no-op, workflow NOT started
# ---------------------------------------------------------------------------


def test_non_labeled_action_ignored(client_and_spy):
    tc, fake = client_and_spy
    payload = {
        "action": "opened",
        "label": {"name": _AGENT_LABEL},
        "repository": {"full_name": _GITHUB_REPO},
        "issue": {"number": 1, "title": "Test"},
    }
    body = json.dumps(payload).encode()
    sig = _sign(body, _SECRET)

    resp = tc.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )

    assert resp.status_code == 200
    assert fake.started == []


# ---------------------------------------------------------------------------
# 4b. Valid sig + action=labeled but label != agent-ready → 200 no-op
# ---------------------------------------------------------------------------


def test_non_agent_label_ignored(client_and_spy):
    tc, fake = client_and_spy
    body = _labeled_payload(label="bug")
    sig = _sign(body, _SECRET)

    resp = tc.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )

    assert resp.status_code == 200
    assert fake.started == []


# ---------------------------------------------------------------------------
# 5. Signature over raw bytes (not re-serialised JSON)
#    Build a payload with non-standard key ordering; if the handler
#    re-serialises before HMAC the signature won't match.
# ---------------------------------------------------------------------------


def test_hmac_computed_over_raw_bytes_not_reserialized(client_and_spy):
    tc, fake = client_and_spy

    # Craft bytes with unusual-but-valid JSON (extra whitespace, specific key order)
    # that json.dumps(json.loads(body)) would NOT reproduce byte-for-byte.
    raw = (
        b'{"action":  "labeled",  '
        b'"label": {"name": "agent-ready"},  '
        b'"repository": {"full_name": "omneval/omneval"},  '
        b'"issue": {"number": 99, "title": "Raw bytes test"}}'
    )
    sig = _sign(raw, _SECRET)

    resp = tc.post(
        "/webhook/github",
        content=raw,
        headers={
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )

    # If HMAC was done over raw bytes the signature is valid → 200 + workflow started
    assert resp.status_code == 200
    assert len(fake.started) == 1


# ---------------------------------------------------------------------------
# 6. Unknown repo → 200 no-op, no crash
# ---------------------------------------------------------------------------


def test_unknown_repo_ignored_gracefully(client_and_spy):
    tc, fake = client_and_spy
    body = _labeled_payload(repo="unknown-org/unknown-repo")
    sig = _sign(body, _SECRET)

    resp = tc.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )

    assert resp.status_code == 200
    assert fake.started == []


# ---------------------------------------------------------------------------
# 7. Non-issues event type → ignored (200)
# ---------------------------------------------------------------------------


def test_non_issues_event_type_ignored(client_and_spy):
    tc, fake = client_and_spy
    payload = {
        "action": "labeled",
        "label": {"name": _AGENT_LABEL},
        "repository": {"full_name": _GITHUB_REPO},
        "issue": {"number": 1, "title": "x"},
    }
    body = json.dumps(payload).encode()
    sig = _sign(body, _SECRET)

    resp = tc.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",  # not "issues"
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )

    assert resp.status_code == 200
    assert fake.started == []


# ---------------------------------------------------------------------------
# 8. No secret configured → signature check skipped (passthrough)
# ---------------------------------------------------------------------------


def test_no_secret_configured_skips_signature_check(client_no_secret):
    tc, fake = client_no_secret
    body = _labeled_payload()
    # No signature header — should still work because no secret is configured

    resp = tc.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "Content-Type": "application/json",
        },
    )

    assert resp.status_code == 200
    assert len(fake.started) == 1


# ---------------------------------------------------------------------------
# pull_request_review / issue_comment → PRCommentWorkflow routing (issue #78)
# ---------------------------------------------------------------------------

_AGENT_PR_BRANCH = "agent/issue-53"
_PR_NUMBER = 17


def _pull_request_review_payload(
    *,
    login: str = "a-human-reviewer",
    head_ref: str = _AGENT_PR_BRANCH,
    repo: str = _GITHUB_REPO,
    body: str = "Please rename this function.",
) -> bytes:
    payload = {
        "action": "submitted",
        "review": {"user": {"login": login}, "body": body, "state": "commented"},
        "pull_request": {
            "number": _PR_NUMBER,
            "head": {"ref": head_ref},
        },
        "repository": {"full_name": repo},
    }
    return json.dumps(payload).encode()


def _issue_comment_payload(
    *,
    login: str = "a-human-reviewer",
    body: str = f"Hey @{_AGENT_LOGIN} can you fix the typo?",
    is_pr: bool = True,
    repo: str = _GITHUB_REPO,
) -> bytes:
    issue: dict = {"number": _PR_NUMBER, "title": "agent: #53 add feature"}
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.com/.../pulls/17"}
    payload = {
        "action": "created",
        "comment": {"user": {"login": login}, "body": body},
        "issue": issue,
        "repository": {"full_name": repo},
    }
    return json.dumps(payload).encode()


def _post_event(tc, body: bytes, event: str):
    sig = _sign(body, _SECRET)
    return tc.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )


def test_pull_request_review_by_human_triggers_pr_comment_workflow(client_and_spy):
    tc, fake = client_and_spy
    body = _pull_request_review_payload(login="a-human-reviewer")

    resp = _post_event(tc, body, "pull_request_review")

    assert resp.status_code == 200
    assert len(fake.started) == 1
    wf = fake.started[0]
    assert wf["workflow"] == "PRCommentWorkflow"
    owner, _, name = _GITHUB_REPO.partition("/")
    assert wf["id"] == f"pr-comment-{owner}-{name}-{_PR_NUMBER}"

    from temporalio.common import WorkflowIDConflictPolicy

    assert wf["id_conflict_policy"] == WorkflowIDConflictPolicy.TERMINATE_EXISTING


def test_pull_request_review_by_bot_is_ignored(client_and_spy):
    tc, fake = client_and_spy
    body = _pull_request_review_payload(login=_AGENT_LOGIN)

    resp = _post_event(tc, body, "pull_request_review")

    assert resp.status_code == 200
    assert fake.started == []


def test_pull_request_review_on_non_agent_branch_is_ignored(client_and_spy):
    tc, fake = client_and_spy
    body = _pull_request_review_payload(head_ref="feature/something-else")

    resp = _post_event(tc, body, "pull_request_review")

    assert resp.status_code == 200
    assert fake.started == []


def test_issue_comment_with_mention_triggers_pr_comment_workflow(client_and_spy):
    tc, fake = client_and_spy
    body = _issue_comment_payload(
        login="a-human-reviewer", body=f"@{_AGENT_LOGIN} please address this"
    )

    resp = _post_event(tc, body, "issue_comment")

    assert resp.status_code == 200
    assert len(fake.started) == 1
    wf = fake.started[0]
    assert wf["workflow"] == "PRCommentWorkflow"
    owner, _, name = _GITHUB_REPO.partition("/")
    assert wf["id"] == f"pr-comment-{owner}-{name}-{_PR_NUMBER}"

    from temporalio.common import WorkflowIDConflictPolicy

    assert wf["id_conflict_policy"] == WorkflowIDConflictPolicy.TERMINATE_EXISTING


def test_issue_comment_without_mention_is_ignored(client_and_spy):
    tc, fake = client_and_spy
    body = _issue_comment_payload(body="Just a regular comment, no mention here.")

    resp = _post_event(tc, body, "issue_comment")

    assert resp.status_code == 200
    assert fake.started == []


def test_issue_comment_by_bot_is_ignored(client_and_spy):
    tc, fake = client_and_spy
    body = _issue_comment_payload(
        login=_AGENT_LOGIN, body=f"@{_AGENT_LOGIN} self-mention from the bot"
    )

    resp = _post_event(tc, body, "issue_comment")

    assert resp.status_code == 200
    assert fake.started == []


def test_issue_comment_on_plain_issue_is_ignored(client_and_spy):
    """A comment on a plain Issue (no `pull_request` key) is not a PR comment —
    PRCommentWorkflow only engages on PRs."""
    tc, fake = client_and_spy
    body = _issue_comment_payload(
        body=f"@{_AGENT_LOGIN} can you look at this?", is_pr=False
    )

    resp = _post_event(tc, body, "issue_comment")

    assert resp.status_code == 200
    assert fake.started == []
