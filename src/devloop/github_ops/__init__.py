"""GitHub operations for devloop.

Re-exports all public activity functions and dataclasses from the
``auth`` and ``operations`` sub-modules so existing imports continue
to work unchanged.

The module-level ``_client()`` and ``_resolve_token()`` helpers are
preserved for internal consumers (e.g. ``summarize_activities``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

# ── Auth re-exports (for backward compatibility with existing tests) ─────

from .auth import (
    GITHUB_API,
    _cached_token_if_fresh,
    _generate_app_jwt,
    _installation_token_cache,
    _installation_token_lock,
    _mint_installation_token,
    _parse_github_timestamp,
    _reset_installation_token_cache,
    auth_client,
    get_installation_token,
    github_app_configured,
)

# ── Operations re-exports ─────────────────────────────────────────────────

from .operations import (
    FileIssuesInput,
    NewIssue,
    _TERMINAL_CONCLUSIONS,
    _async_resolve,
    _headers,
    _log_github_api_failure,
    agent_pr_issue_numbers,
    create_github_issue,
    file_issues,
    get_pr_branch,
    get_pr_diff,
    make_issue_branch,
    open_agent_pr_issue_numbers,
    plan_issue,
    poll_ci_checks,
    post_github_comment,
    post_pr_comments,
    request_github_reviewer,
    update_github_issue,
)

# ── Backward-compat re-exports ────────────────────────────────────────────

# These symbols lived in the old monolithic github_ops.py.  Re-export them
# from the package so existing imports and monkeypatch targets work unchanged.

from ..github import PlanIssueInput
from ..projects import ProjectConfig, get_project


def _make_issue_branch(issue_number: int, title: str) -> str:
    """Backward-compat alias for ``make_issue_branch``."""
    return make_issue_branch(issue_number, title)


def _github_app_configured() -> bool:
    """Backward-compat alias for ``github_app_configured``."""
    return github_app_configured()


# ── Public activity functions (Temporal activities) ──────────────────────

# These are the actual @activity.defn-decorated functions that Temporal
# workflows call.  They stay as re-exports so existing workflow code
# ``from devloop.github_ops import poll_ci_checks`` works unchanged.

__all__ = [
    # Auth internals (backward compat)
    "GITHUB_API",
    "_cached_token_if_fresh",
    "_generate_app_jwt",
    "_github_app_configured",
    "_installation_token_cache",
    "_installation_token_lock",
    "_mint_installation_token",
    "_parse_github_timestamp",
    "_reset_installation_token_cache",
    "auth_client",
    "get_installation_token",
    "github_app_configured",
    # Operations
    "FileIssuesInput",
    "NewIssue",
    "_TERMINAL_CONCLUSIONS",
    "_async_resolve",
    "_headers",
    "_log_github_api_failure",
    "agent_pr_issue_numbers",
    "create_github_issue",
    "file_issues",
    "get_pr_branch",
    "get_pr_diff",
    "make_issue_branch",
    "open_agent_pr_issue_numbers",
    "plan_issue",
    "poll_ci_checks",
    "post_github_comment",
    "post_pr_comments",
    "request_github_reviewer",
    "update_github_issue",
    # Backward-compat shim (summarize_activities imports _client)
    "_client",
    "_make_issue_branch",
    "_resolve_token",
    # Backward-compat re-exports
    "PlanIssueInput",
    "get_project",
]


# ── Backward-compat shims ────────────────────────────────────────────────

# summarize_activities.py imports ``_client`` from github_ops.
# We keep this shim so existing imports work.


async def _resolve_token(cfg: ProjectConfig, token: str | None = None) -> str:
    """Resolve the GitHub credential to use for ``cfg``.

    When ``token`` is provided, use it directly — no auth or cluster calls
    are made.  Otherwise: when the GitHub App is configured use an
    installation token; otherwise fall back to the project's scoped PAT or
    the local ``GITHUB_TOKEN`` env var.
    """
    if token:
        return token

    import os

    from . import auth

    if auth.github_app_configured():
        return await auth.get_installation_token()
    if not cfg.github_token_secret:
        # Local quickstart (issue #116): fall back to the worker's own
        # GITHUB_TOKEN env var instead of reaching for the Kubernetes API.
        return os.environ.get("GITHUB_TOKEN", "")
    from .. import cluster

    return cluster.read_secret_value(cfg.github_token_secret, "GITHUB_TOKEN")


async def _client(
    cfg: ProjectConfig,
    extra_headers: dict[str, str] | None = None,
    token: str | None = None,
) -> httpx.Client:
    """Build an authenticated httpx.Client for ``cfg``.

    Backward-compat shim: existing code uses ``with await _client(cfg) as c:``.

    ``extra_headers`` allows callers to add custom headers (e.g. Accept)
    on top of the auth and standard GitHub headers.

    ``token`` allows callers to supply a fake token directly (useful for
    tests) — when provided, auth resolution is skipped entirely.
    """
    import httpx

    resolved_token = await _resolve_token(cfg, token=token)
    headers: dict[str, str] = {
        "Authorization": f"Bearer {resolved_token}",
        **_headers(resolved_token),
    }
    if extra_headers:
        headers.update(extra_headers)
    return httpx.Client(
        base_url=GITHUB_API,
        headers=headers,
        timeout=30.0,
    )
