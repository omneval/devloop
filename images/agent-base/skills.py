"""Skill resolution module for Agent Execution Jobs (issues #32, #35).

Resolves installed skills from the convergence directory and applies a
per-phase allowlist so each Agent Execution Job phase receives only the
skills it is permitted to use.

Convergence directory
---------------------
``AGENT_SKILLS_DIR`` env var → ``/usr/local/share/agent-skills/installed``

This mirrors the prompt-template convention (``AGENT_PROMPTS_DIR`` →
``/usr/local/share/agent-prompts``) so the two resource trees are deployed
side-by-side and both are overridable in tests.

Allowlist semantics
-------------------
``allowlist`` is an optional ``{phase: list[str] | []}`` mapping.

- Key absent  → all installed skills returned (no filtering for this phase).
- Key = ``[]`` → no skills returned (phase explicitly allows nothing).
- Key = names  → exactly those installed skills whose ``.name`` is listed.

Skills whose name is empty are always skipped and reported regardless of
the allowlist, because an un-named skill cannot be matched or used.

Skipped-skills report (issue #35)
----------------------------------
``resolve_skills`` returns a list of ``{"name": str, "reason": str}`` dicts.
Pass this to ``format_skipped_notice`` to get a one-line human-readable
string suitable for appending to a phase summary.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    # Avoid importing openhands at module load time so tests that don't need
    # the SDK can import skills.py without the package installed.
    pass

log = logging.getLogger(__name__)

_DEFAULT_SKILLS_DIR = "/usr/local/share/agent-skills/installed"


def _default_loader(installed_dir: Path) -> list[Any]:
    """Load installed skills from *installed_dir* via the openhands SDK."""
    from openhands.sdk.skills.installed import load_installed_skills

    return load_installed_skills(installed_dir=installed_dir)


def format_skipped_notice(skipped: list[dict]) -> str:
    """Format a one-line human-readable notice about skipped skills.

    Returns an empty string when *skipped* is empty (no notice appended to
    the phase summary — backward-compatible with deployments that have no
    skipped skills).

    Parameters
    ----------
    skipped:
        List of ``{"name": str, "reason": str}`` dicts from ``resolve_skills``.

    Returns
    -------
    str
        E.g. ``"⚠ 2 skill(s) not loaded: foo (malformed: no name), bar (not in allowlist)"``
        or ``""`` when skipped is empty.
    """
    if not skipped:
        return ""
    parts = [
        f"{s['name']} ({s['reason']})" if s["name"] else f"(unnamed: {s['reason']})"
        for s in skipped
    ]
    n = len(skipped)
    return f"⚠ {n} skill(s) not loaded: {', '.join(parts)}"


def resolve_skills(
    phase: str,
    allowlist: dict | None,
    *,
    _loader: Callable[[Path], list[Any]] | None = None,
) -> tuple[list[Any], list[dict]]:
    """Return ``(resolved_skills, skipped_report)`` for *phase*.

    Parameters
    ----------
    phase:
        Current agent phase name (e.g. ``"execute"``, ``"review"``).
    allowlist:
        Optional ``{phase: list[str] | []}`` mapping.  Pass ``None`` to allow
        all installed skills for every phase.
    _loader:
        Injected loader for testing.  Defaults to the real
        ``load_installed_skills`` call.

    Returns
    -------
    resolved_skills:
        List of ``Skill`` objects (or loader-returned objects) that pass all
        filters.
    skipped_report:
        List of ``{"name": str, "reason": str}`` dicts describing each
        skipped entry.  Pass to ``format_skipped_notice`` to produce the
        one-line phase-summary notice (issue #35).
    """
    if _loader is None:
        _loader = _default_loader

    skills_dir_env = os.environ.get("AGENT_SKILLS_DIR")
    installed_dir = Path(skills_dir_env if skills_dir_env else _DEFAULT_SKILLS_DIR)

    raw: list[Any] = _loader(installed_dir)

    resolved: list[Any] = []
    skipped: list[dict] = []

    # Step 1: filter malformed (un-named) skills
    valid: list[Any] = []
    for skill in raw:
        name = getattr(skill, "name", None)
        if not name:
            skipped.append({"name": "", "reason": "malformed: no name attribute"})
            log.warning("skipping unnamed skill: %r", skill)
        else:
            valid.append(skill)

    # Step 2: apply per-phase allowlist
    if allowlist is None or phase not in allowlist:
        # No restriction for this phase — all valid skills pass through
        resolved = valid
    else:
        permitted: list[str] = allowlist[phase]
        if not permitted:
            # Explicit empty list → no skills for this phase
            resolved = []
            # Don't report valid skills as "skipped" — they're not in scope
        else:
            permitted_set = set(permitted)
            for skill in valid:
                if skill.name in permitted_set:
                    resolved.append(skill)
                else:
                    skipped.append(
                        {
                            "name": skill.name,
                            "reason": f"not in allowlist for phase {phase!r}",
                        }
                    )
                    log.debug(
                        "skill %r not in allowlist for phase %r — skipping",
                        skill.name,
                        phase,
                    )

    log.info(
        "resolved %d skill(s) for phase %r; skipped %d",
        len(resolved),
        phase,
        len(skipped),
    )
    return resolved, skipped


def install_configmap_skills(staging_path: str) -> list[str]:
    """Install skills from a ConfigMap staging directory into the convergence dir.

    Stage-and-install design (ADR-0008): the ConfigMap is mounted at a separate
    read-only staging path rather than directly at the convergence directory.
    Mounting a Kubernetes volume at the convergence directory would replace its
    contents and hide every baked skill — stage-and-install avoids this while
    letting ConfigMap-delivered skills override baked skills by name.

    Each key in the ConfigMap is a skill name; the value is the ``SKILL.md``
    content.  The staging directory contains one file per skill (the file name
    is the skill name); each file is written as
    ``<convergence>/<skill_name>/SKILL.md``.

    ConfigMap-delivered skills win on name collision: an existing entry in the
    convergence directory is overwritten.

    Parameters
    ----------
    staging_path:
        Directory (read-only ConfigMap mount) containing one file per skill.

    Returns
    -------
    list[str]
        Names of skills that were successfully installed.
    """
    staging = Path(staging_path)
    if not staging.is_dir():
        log.debug(
            "staging path %s does not exist or is not a directory — skipping", staging
        )
        return []

    skills_dir_env = os.environ.get("AGENT_SKILLS_DIR")
    convergence = Path(skills_dir_env if skills_dir_env else _DEFAULT_SKILLS_DIR)

    installed: list[str] = []
    for entry in sorted(staging.iterdir()):
        if entry.is_file():
            # Flat-file layout (e.g. staging/tdd) — copy directly as SKILL.md
            skill_name = entry.name
            dest_dir = convergence / skill_name
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / "SKILL.md"
            try:
                shutil.copy2(entry, dest)
                installed.append(skill_name)
                log.info("installed ConfigMap skill %r → %s", skill_name, dest)
            except OSError as exc:
                log.warning(
                    "failed to write SKILL.md for skill %r: %s — skipping",
                    skill_name,
                    exc,
                )
        elif entry.is_dir():
            # ConfigMap mount layout: staging/<name>/SKILL.md created by K8s
            # when the ConfigMap key contains a slash.
            skill_md = entry / "SKILL.md"
            if skill_md.is_file():
                skill_name = entry.name
                dest_dir = convergence / skill_name
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / "SKILL.md"
                try:
                    shutil.copy2(skill_md, dest)
                    installed.append(skill_name)
                    log.info("installed ConfigMap skill %r → %s", skill_name, dest)
                except OSError as exc:
                    log.warning(
                        "failed to write SKILL.md for skill %r: %s — skipping",
                        skill_name,
                        exc,
                    )
    return installed
