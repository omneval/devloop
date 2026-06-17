"""CI check polling types for github_ops CI check polling.

Contains the dataclasses used by the ``poll_ci_checks`` activity
and consumed by ``Phase.CI_FIX`` so the fix agent knows exactly
what to address.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PollCIChecksInput:
    """Input for the poll_ci_checks activity.

    Polls the GitHub Checks/Status APIs for the head commit of the given PR
    (``gh pr checks`` equivalent) and reports whether every check has passed,
    plus details on any that failed — consumed by ``Phase.CI_FIX`` so the fix
    Agent Execution Job knows exactly what to address.
    """

    project_id: str
    pr_number: int


@dataclass
class CICheckFailure:
    """One failing CI check, as surfaced to the ci_fix Agent Execution Job via
    ``TaskSpec.extra["ci_check_failures"]``."""

    name: str
    conclusion: str = ""
    details_url: str = ""
    summary: str = ""


@dataclass
class CIChecksResult:
    """The poll_ci_checks activity's result: pass/fail plus failure details.

    ``pending`` distinguishes "still running — wait and re-poll" from
    "genuinely failing — dispatch a fix": it's ``True`` only when no check
    has actually failed yet but at least one is still queued/in-progress (or
    the poll itself couldn't be completed, e.g. a transient GitHub-side
    error). ``all_passed`` can be ``False`` while ``pending`` is ``True`` and
    ``failures`` is empty — that combination means "not done yet", not "red"
    (issue #90).
    """

    all_passed: bool = False
    pending: bool = False
    failures: list[CICheckFailure] = field(default_factory=list)
