"""Pure helpers for the Dev Loop workflow.

No Temporal / I/O imports — safe to use from both the workflow sandbox and
unit tests.
"""

from __future__ import annotations

import re

from .shared import Verdict

_APPROVE_TOKENS = ("approve", "approved", "yes", "lgtm", "✅", "👍")


def is_approval(reply: str) -> bool:
    """True if a Discord reply approves a Phase Gate."""
    low = (reply or "").strip().lower()
    if not low:
        return False
    return any(tok in low for tok in _APPROVE_TOKENS)


def parse_merge_reply(reply: str, reviewed: list[tuple[int, str]]) -> list[int]:
    """Resolve which issue numbers to merge from a Discord reply.

    Retained for callers that gate several branches at once; the sequential Dev
    Loop gates one issue per round and uses :func:`is_approval` instead.

    * "all passed" → every branch whose verdict is ``pass`` or ``warn``.
    * otherwise → the explicit issue numbers named in the reply.
    """
    low = (reply or "").strip().lower()
    reviewed_map = dict(reviewed)
    if "all passed" in low or low in {"all", "merge all"}:
        return [n for n, v in reviewed if v in (Verdict.PASS.value, Verdict.WARN.value)]
    wanted = {int(m) for m in re.findall(r"#?(\d+)", low)}
    return [n for n in reviewed_map if n in wanted]


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
