"""Default agent skills and user-extensible skill installation (issue #20).

Provides functions to:
- List available skills in a directory
- Resolve effective skills by merging baked-in defaults with project-level overrides
- Install custom skills via the npx skills CLI

Default skills are baked into the agent-base image at DEFAULT_SKILLS_DIR.
Project-level skills live under ``<workdir>/.agents/skills/`` and override
defaults by name.  An empty project skill removes the default.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Baked-in default skills installed during Docker build.
DEFAULT_SKILLS_DIR: str = "/usr/local/share/agent-skills"

#: Standard OpenHands skills directory inside a project workspace.
PROJECT_SKILLS_SUFFIX: str = ".agents/skills"

#: The markdown file that defines a skill's instructions.
SKILL_FILENAME: str = "SKILL.md"


# --------------------------------------------------------------------------- #
# Skill enumeration
# --------------------------------------------------------------------------- #


def list_skills(directory: str | Path) -> list[str]:
    """Return the names of skill directories inside *directory*.

    Only first-level sub-directories are considered; files and nested
    directories are ignored.
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        return []

    return sorted(
        entry.name
        for entry in dir_path.iterdir()
        if entry.is_dir()
    )


def _read_skill(skill_dir: Path) -> str:
    """Read the ``SKILL.md`` content from *skill_dir*, or ``""`` if absent."""
    skill_file = skill_dir / SKILL_FILENAME
    if skill_file.is_file():
        return skill_file.read_text()
    return ""


# --------------------------------------------------------------------------- #
# Skill resolution
# --------------------------------------------------------------------------- #


def resolve_effective_skills(
    workdir: str | Path,
    default_skills_dir: str | Path = DEFAULT_SKILLS_DIR,
) -> dict[str, str]:
    """Merge baked-in default skills with project-level overrides.

    Parameters
    ----------
    workdir:
        The project workspace root.  Project-level skills are expected
        under ``<workdir>/.agents/skills/``.
    default_skills_dir:
        Directory containing baked-in default skills from the Docker
        image.  Defaults to :data:`DEFAULT_SKILLS_DIR`.

    Returns
    -------
    dict[str, str]
        Mapping of skill name to the content of its ``SKILL.md``.
        Project skills override defaults by name.  An empty project
        skill removes the default (key is absent from the result).

    Resolution order (last-write-wins):
    1. Baked-in defaults from *default_skills_dir*
    2. Project-level skills from *workdir* ``.agents/skills/``
    """
    workdir = Path(workdir)
    default_dir = Path(default_skills_dir)

    skills: dict[str, str] = {}
    if default_dir.is_dir():
        for name in list_skills(default_dir):
            content = _read_skill(default_dir / name)
            if content:
                skills[name] = content

    project_skills_dir = workdir / PROJECT_SKILLS_SUFFIX
    if project_skills_dir.is_dir():
        for name in list_skills(project_skills_dir):
            content = _read_skill(project_skills_dir / name)
            if content:
                skills[name] = content
            else:
                skills.pop(name, None)

    return skills


# --------------------------------------------------------------------------- #
# Skill installation (uses npx skills CLI)
# --------------------------------------------------------------------------- #


def _run(cmd: list[str], cwd: str | None = None) -> str:
    """Run a command and return its stdout."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Command {' '.join(cmd)} failed: {result.stderr.strip()}"
        )
    return result.stdout


def install_skills(
    target_dir: str | Path,
    skills: list[dict[str, str]],
    agent: str = "openhands",
) -> list[str]:
    """Install skills using the ``npx skills`` CLI.

    Parameters
    ----------
    target_dir:
        Directory where skills will be installed.
    skills:
        List of skill specs, each with at least ``"repo"`` (owner/repo)
        and ``"skill"`` (skill name).  Example::

            [{"repo": "mattpocock/skills", "skill": "tdd"}]

    agent:
        Target agent identifier passed to ``npx skills add -a <agent>``.
        Defaults to ``"openhands"``.

    Returns
    -------
    list[str]
        Names of successfully installed skills.
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []

    for skill_spec in skills:
        repo = skill_spec.get("repo", "")
        skill_name = skill_spec.get("skill", "")

        if not repo or not skill_name:
            continue

        cmd = [
            "npx",
            "skills@latest",
            "add",
            repo,
            "--skill",
            skill_name,
            "-a",
            agent,
            "-y",
        ]

        _run(cmd, cwd=str(target_dir))
        installed.append(skill_name)

    return installed
