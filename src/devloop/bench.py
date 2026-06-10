"""devloop bench — golden-issue replay scored by an LLM judge (issue #122).

Turns prompt/model/harness changes into measured A/B decisions instead of
anecdotes: replay a *golden set* of already-closed issues against whatever the
current deployment runs, then score each resulting agent PR with an LLM judge
against the issue's acceptance criteria and the historical human-merged PR.

    devloop-bench --project omneval --issues 67,68 \
        --scratch-repo your-org/devloop-bench-omneval \
        --report bench-report.json

Per golden issue, the flow is:

1. **Fetch the golden spec** from the source repo: the issue title/body and
   the merged PR that closed it (the human baseline diff).
2. **Replay**: open a copy of the issue on the scratch repo and apply the
   agent label. The scratch repo must be enrolled in devloop (webhook +
   registry entry) — labeling is the trigger, exactly like production. Then
   poll until an ``agent/issue-<N>`` PR appears (or the timeout passes) and
   fetch its diff.
3. **Judge**: one LLM call scoring the agent diff against the acceptance
   criteria and the human baseline. The judge model resolves via the per-role
   LLM settings: ``AGENT_MODEL_JUDGE`` → ``AGENT_MODEL_REVIEW`` →
   ``AGENT_MODEL`` (likewise ``AGENT_LLM_BASE_URL*`` / ``AGENT_LLM_API_KEY*``).

``--no-replay`` skips step 2 and judges the *golden* PR itself — useful for
calibrating the judge (a sane judge should score the merged human PR highly).

Network access is isolated behind module-level seams (``_github_request``,
``_judge_client``) so the whole flow is unit-testable without GitHub or an
LLM endpoint.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import re
import sys
import time

log = logging.getLogger(__name__)

GITHUB_API = os.getenv("GITHUB_API", "https://api.github.com")

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "criteria_total": {"type": "integer"},
        "criteria_met": {"type": "integer"},
        "score": {"type": "integer", "minimum": 0, "maximum": 10},
        "rationale": {"type": "string"},
    },
    "required": ["criteria_total", "criteria_met", "score", "rationale"],
    "additionalProperties": False,
}

_JUDGE_SYSTEM = """\
You are scoring an autonomous coding agent's pull request against the GitHub
issue it implements. You are given the issue (including its acceptance
criteria), the agent's diff, and — when available — the diff of the
historical human-authored PR that actually closed the issue (the baseline).

Score strictly:
- criteria_total: how many distinct acceptance criteria the issue states.
- criteria_met: how many of those the agent's diff plausibly satisfies.
- score: 0-10 overall. 8+ means mergeable as-is; 5-7 means right direction
  but needs human fixes; <5 means off the mark. Judge substance, not style —
  a different-but-valid approach to the human baseline is fine.
- rationale: 2-4 sentences citing the specific criteria that are met/unmet.

Respond with JSON only."""


# --------------------------------------------------------------------------- #
# Seams (mocked in tests)
# --------------------------------------------------------------------------- #
def _github_request(method: str, url: str, *, accept: str = "", **kwargs):
    """Single GitHub HTTP seam. Returns the ``httpx.Response``."""
    import httpx

    headers = {
        "Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN', '')}",
        "Accept": accept or "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = httpx.request(method, url, headers=headers, timeout=30.0, **kwargs)
    resp.raise_for_status()
    return resp


def _judge_client(base_url: str, api_key: str):
    """LLM client seam for the judge."""
    from openai import OpenAI

    return OpenAI(base_url=base_url, api_key=api_key or "local")


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def _judge_setting(name: str, default: str = "") -> str:
    """Resolve a judge LLM setting: JUDGE role → REVIEW role → base env."""
    for suffix in ("_JUDGE", "_REVIEW", ""):
        val = os.environ.get(f"{name}{suffix}")
        if val:
            return val
    return default


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class GoldenIssue:
    number: int
    title: str
    body: str
    human_pr_number: int = 0
    human_diff: str = ""


@dataclasses.dataclass
class IssueScore:
    issue_number: int
    title: str
    replay_issue_number: int = 0
    agent_pr_number: int = 0
    criteria_total: int = 0
    criteria_met: int = 0
    score: int = 0
    rationale: str = ""
    error: str = ""


# --------------------------------------------------------------------------- #
# Step 1 — golden spec
# --------------------------------------------------------------------------- #
def fetch_golden(repo: str, number: int) -> GoldenIssue:
    """Fetch the issue and its closing (merged) PR from the source repo."""
    issue = _github_request("GET", f"{GITHUB_API}/repos/{repo}/issues/{number}").json()
    golden = GoldenIssue(
        number=number, title=issue.get("title", ""), body=issue.get("body") or ""
    )

    # The merged PR that references this issue, newest first.
    search = _github_request(
        "GET",
        f"{GITHUB_API}/search/issues",
        params={
            "q": f"repo:{repo} is:pr is:merged {number} in:body",
            "sort": "created",
            "order": "desc",
        },
    ).json()
    for item in search.get("items", []):
        pr_number = int(item.get("number", 0))
        if pr_number and pr_number != number:
            golden.human_pr_number = pr_number
            golden.human_diff = _github_request(
                "GET",
                f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
                accept="application/vnd.github.diff",
            ).text
            break
    return golden


# --------------------------------------------------------------------------- #
# Step 2 — replay on the scratch repo
# --------------------------------------------------------------------------- #
def open_replay_issue(scratch_repo: str, golden: GoldenIssue, label: str) -> int:
    """Open a copy of the golden issue on the scratch repo and apply the
    trigger label (the label event starts the Dev Loop via the webhook,
    exactly like production)."""
    created = _github_request(
        "POST",
        f"{GITHUB_API}/repos/{scratch_repo}/issues",
        json={
            "title": f"[bench #{golden.number}] {golden.title}",
            "body": golden.body,
        },
    ).json()
    replay_number = int(created["number"])
    _github_request(
        "POST",
        f"{GITHUB_API}/repos/{scratch_repo}/issues/{replay_number}/labels",
        json={"labels": [label]},
    )
    return replay_number


_AGENT_BRANCH = re.compile(r"^agent/issue-(\d+)")


def find_agent_pr(scratch_repo: str, replay_number: int) -> int:
    """PR number whose head branch is ``agent/issue-<replay_number>``, or 0."""
    pulls = _github_request(
        "GET",
        f"{GITHUB_API}/repos/{scratch_repo}/pulls",
        params={"state": "all", "per_page": 100},
    ).json()
    for pr in pulls:
        ref = (pr.get("head") or {}).get("ref", "")
        m = _AGENT_BRANCH.match(ref)
        if m and int(m.group(1)) == replay_number:
            return int(pr.get("number", 0))
    return 0


def await_agent_pr(
    scratch_repo: str,
    replay_number: int,
    timeout_seconds: float,
    poll_seconds: float,
    *,
    _sleep=time.sleep,
    _clock=time.monotonic,
) -> int:
    """Poll until the agent PR for the replayed issue appears; 0 on timeout."""
    deadline = _clock() + timeout_seconds
    while True:
        pr = find_agent_pr(scratch_repo, replay_number)
        if pr:
            return pr
        if _clock() >= deadline:
            return 0
        _sleep(poll_seconds)


def fetch_pr_diff(repo: str, pr_number: int) -> str:
    return _github_request(
        "GET",
        f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
        accept="application/vnd.github.diff",
    ).text


# --------------------------------------------------------------------------- #
# Step 3 — judge
# --------------------------------------------------------------------------- #
def judge_diff(golden: GoldenIssue, agent_diff: str) -> dict:
    """One LLM call scoring the agent diff. Returns the parsed JSON verdict."""
    model = _judge_setting("AGENT_MODEL")
    if not model:
        raise RuntimeError(
            "no judge model configured — set AGENT_MODEL_JUDGE, "
            "AGENT_MODEL_REVIEW, or AGENT_MODEL"
        )
    client = _judge_client(
        _judge_setting("AGENT_LLM_BASE_URL"),
        _judge_setting("AGENT_LLM_API_KEY"),
    )
    baseline = (
        f"\n\n## Human baseline PR diff (merged)\n```diff\n{golden.human_diff}\n```"
        if golden.human_diff
        else "\n\n(No human baseline PR was found for this issue.)"
    )
    user = (
        f"## Issue #{golden.number}: {golden.title}\n\n{golden.body}"
        f"\n\n## Agent PR diff\n```diff\n{agent_diff}\n```"
        f"{baseline}"
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": user},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "bench_score", "schema": _JUDGE_SCHEMA},
        },
    )
    return json.loads(resp.choices[0].message.content)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def bench_issue(
    source_repo: str,
    scratch_repo: str,
    number: int,
    *,
    label: str,
    timeout_seconds: float,
    poll_seconds: float,
    replay: bool = True,
) -> IssueScore:
    golden = fetch_golden(source_repo, number)
    score = IssueScore(issue_number=number, title=golden.title)
    try:
        if replay:
            score.replay_issue_number = open_replay_issue(scratch_repo, golden, label)
            score.agent_pr_number = await_agent_pr(
                scratch_repo, score.replay_issue_number, timeout_seconds, poll_seconds
            )
            if not score.agent_pr_number:
                score.error = (
                    f"timed out waiting for agent PR on "
                    f"{scratch_repo}#{score.replay_issue_number}"
                )
                return score
            diff = fetch_pr_diff(scratch_repo, score.agent_pr_number)
        else:
            # Judge calibration: score the human-merged PR itself.
            if not golden.human_diff:
                score.error = "no merged PR found to judge (--no-replay)"
                return score
            score.agent_pr_number = golden.human_pr_number
            diff = golden.human_diff

        verdict = judge_diff(golden, diff)
        score.criteria_total = int(verdict.get("criteria_total", 0))
        score.criteria_met = int(verdict.get("criteria_met", 0))
        score.score = int(verdict.get("score", 0))
        score.rationale = verdict.get("rationale", "")
    except Exception as exc:  # noqa: BLE001 — one bad issue must not sink the run
        log.exception("bench failed for issue #%d", number)
        score.error = str(exc)
    return score


def resolve_source_repo(project: str, projects_file: str) -> str:
    """``--project`` is either an ``OWNER/REPO`` slug or a Project Registry id
    resolved via the projects file (PROJECTS_FILE / --projects-file)."""
    if "/" in project:
        return project
    from .projects import load_projects, parse_github_repo

    for cfg in load_projects(projects_file):
        if cfg.id == project:
            return parse_github_repo(cfg.github_url)
    raise SystemExit(
        f"project {project!r} not found in {projects_file!r} — pass either a "
        "registry id (with PROJECTS_FILE/--projects-file set) or OWNER/REPO"
    )


def format_report(scores: list[IssueScore]) -> str:
    lines = [
        f"{'issue':>7}  {'replay':>7}  {'PR':>6}  {'criteria':>9}  {'score':>5}  result",
        "-" * 72,
    ]
    for s in scores:
        criteria = f"{s.criteria_met}/{s.criteria_total}" if s.criteria_total else "-"
        result = s.error or s.rationale.split("\n")[0][:60]
        lines.append(
            f"#{s.issue_number:>6}  {s.replay_issue_number or '-':>7}  "
            f"{s.agent_pr_number or '-':>6}  {criteria:>9}  {s.score:>5}  {result}"
        )
    scored = [s for s in scores if not s.error]
    if scored:
        mean = sum(s.score for s in scored) / len(scored)
        lines.append("-" * 72)
        lines.append(f"mean score: {mean:.1f}/10 over {len(scored)} issue(s)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="devloop-bench", description="Golden-issue replay scored by an LLM judge"
    )
    parser.add_argument(
        "--project",
        required=True,
        help="Project Registry id (resolved via --projects-file) or OWNER/REPO",
    )
    parser.add_argument(
        "--issues",
        required=True,
        help="comma-separated golden issue numbers, e.g. 67,68",
    )
    parser.add_argument(
        "--scratch-repo",
        default="",
        help="OWNER/REPO of the devloop-enrolled scratch repo replays run on "
        "(required unless --no-replay)",
    )
    parser.add_argument("--agent-label", default="agent-ready")
    parser.add_argument("--timeout-minutes", type=float, default=90.0)
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument(
        "--projects-file", default=os.environ.get("PROJECTS_FILE", "./projects.yaml")
    )
    parser.add_argument(
        "--report", default="", help="write the full JSON report to this path"
    )
    parser.add_argument(
        "--no-replay",
        action="store_true",
        help="skip the replay and judge the historical human PR (judge calibration)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not args.no_replay and not args.scratch_repo:
        parser.error("--scratch-repo is required unless --no-replay is set")
    if not os.environ.get("GITHUB_TOKEN"):
        parser.error("GITHUB_TOKEN must be set")

    source_repo = resolve_source_repo(args.project, args.projects_file)
    numbers = [int(n) for n in args.issues.split(",") if n.strip()]

    scores = [
        bench_issue(
            source_repo,
            args.scratch_repo,
            n,
            label=args.agent_label,
            timeout_seconds=args.timeout_minutes * 60,
            poll_seconds=args.poll_seconds,
            replay=not args.no_replay,
        )
        for n in numbers
    ]

    print(format_report(scores))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump([dataclasses.asdict(s) for s in scores], f, indent=2)
        print(f"\nfull report written to {args.report}")
    return 0 if all(not s.error for s in scores) else 1


if __name__ == "__main__":
    sys.exit(main())
