"""Pure helpers for the Dev Loop workflow.

No Temporal / I/O imports — safe to use from both the workflow sandbox and
unit tests.

Exposed utilities
------------------
* ``is_approval(reply)`` — recognise common Phase Gate approval tokens
  (``approve``, ``lgtm``, ``👍``, …) in a reply string.
* ``pr_number_from_url(pr_url)`` — extract the ``<N>`` from a GitHub ``/pull/<N>``
  URL fragment; returns ``0`` when no number is found.
* ``render_plan(project_id, iteration, issues)`` — produce the Plan-gate
  message that tells a human which issue is next and what other candidates
  are unblocked for the round.
* ``merge_gate_message(issue, pr_url)`` — produce the per-issue Merge-gate
  prompt that asks whether to open a review PR or skip.
* ``render_review_findings_comment(summary, inline_comments)`` — render
  reviewer findings as a single plain-text comment, for posting to the issue
  when no PR exists to anchor inline comments to.
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


def render_review_findings_comment(summary: str, inline_comments: list) -> str:
    """Render reviewer findings as a single plain-text comment.

    Used when there is no PR to anchor inline (line-level) comments to —
    e.g. ``create_pr`` failed best-effort and only pushed the branch (see
    ``create_pr``'s docstring in ``entrypoint.py``). Each inline comment is
    rendered as a ``file:line`` bullet so the finding still surfaces instead
    of being silently dropped. ``inline_comments`` holds ``InlineComment``
    instances (``.file``, ``.line``, ``.body``).
    """
    lines = ["### Agent review", ""]
    if summary:
        lines.append(summary)
    if inline_comments:
        lines.append("")
        lines.append(
            "_No PR was opened for this branch, so inline comments are "
            "listed here instead:_"
        )
        for c in inline_comments:
            lines.append(f"- `{c.file}:{c.line}` — {c.body}")
    return "\n".join(lines)
