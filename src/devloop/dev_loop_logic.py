"""Pure helpers for the Dev Loop workflow.

No Temporal / I/O imports — safe to use from both the workflow sandbox and
unit tests.
"""

from __future__ import annotations

import re

_APPROVE_TOKENS = ("approve", "approved", "yes", "lgtm", "✅", "👍")
_PR_NUMBER = re.compile(r"/pull/(\d+)")


def is_approval(reply: str) -> bool:
    """True if a reply approves a Phase Gate."""
    low = (reply or "").strip().lower()
    if not low:
        return False
    return any(tok in low for tok in _APPROVE_TOKENS)


def pr_number_from_url(pr_url: str) -> int:
    """Extract the PR number from a GitHub PR URL (``…/pull/<N>``); 0 if absent."""
    m = _PR_NUMBER.search(pr_url or "")
    return int(m.group(1)) if m else 0


# --------------------------------------------------------------------------- #
# Sequential Dev Loop rendering (one issue per round)
# --------------------------------------------------------------------------- #
def render_plan(project_id: str, iteration: int, issues: list[dict]) -> str:
    """Render the Plan gate for a round: the issue about to be worked plus the
    other unblocked candidates the planner surfaced.

    ``issues`` is the planner's ``<plan>`` list ([{id, title, branch}, …]); the
    workflow works ``issues[0]`` next.
    """
    if not issues:
        return f"_No unblocked issues for `{project_id}` this round._"
    nxt = issues[0]
    lines = [
        f"**Dev Loop `{project_id}` — round {iteration}**",
        "",
        f"Next up: **#{nxt.get('id')} — {nxt.get('title', '')}** "
        f"→ `{nxt.get('branch', '')}`",
    ]
    if len(issues) > 1:
        lines += ["", "Other unblocked candidates this round:"]
        lines += [f"- #{i.get('id')} — {i.get('title', '')}" for i in issues[1:]]
    lines += [
        "",
        "Reply **approve** to implement it, or reply with feedback to re-plan.",
    ]
    return "\n".join(lines)


def merge_gate_message(issue: dict, pr_url: str) -> str:
    """Render the per-issue Merge gate prompt."""
    where = pr_url or "branch pushed (no PR link)"
    return (
        f"**Merge gate — #{issue.get('id')} {issue.get('title', '')}**\n"
        f"{where}\n\n"
        "Reply **approve** to open a review PR (tagging the reviewer for the "
        "final review + merge on GitHub), or anything else to skip."
    )
