"""Tests for skills.py — skill resolution module (issue #32).

Design: ``resolve_skills`` is tested by injecting a fake ``_loader`` so tests
don't need the real ``openhands-sdk`` on the path and don't touch the filesystem
beyond what they control.  A ``Skill``-like namedtuple stands in so we can
construct test fixtures without importing the SDK.

The one filesystem-touching test (``AGENT_SKILLS_DIR`` env override) also injects
the loader, so it only needs to know which ``Path`` was passed to the loader.

Filesystem tests exercise the real ``load_installed_skills`` loader from
openhands-sdk 1.24.0 with temp directories containing real SKILL.md files.
These verify the end-to-end baked-skills path: disk → SDK loader → resolve_skills
→ (skills, skipped_report).
"""

from __future__ import annotations

from collections import namedtuple
from pathlib import Path

# ---- Minimal Skill stand-in (tests don't need the real SDK object) ----------
FakeSkill = namedtuple("FakeSkill", ["name", "content"])


# ---- Helper: import-under-test regardless of cwd ---------------------------
def _import_skills():
    import importlib
    import sys

    # The module lives next to the test file; ensure it's importable.
    parent = str(Path(__file__).parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    # Reuse the existing module if already loaded. resolve_skills reads
    # AGENT_SKILLS_DIR at call time (not import time), so monkeypatch env
    # changes are picked up without needing a fresh import.
    if "skills" not in sys.modules:
        return importlib.import_module("skills")

    return sys.modules["skills"]


# ---- SKILL.md templates for filesystem tests ------------------------------

_VALID_SKILL_MD = """\
---
name: {name}
description: A test skill
triggers:
  - test
---

# Test Skill

Content for {name}.
"""


def _write_skill(tmp_path: Path, name: str) -> Path:
    """Create a valid SKILL.md in a subdirectory of tmp_path."""
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(_VALID_SKILL_MD.format(name=name))
    return skill_dir


# ============================================================================
# Tracer bullet: empty loader → empty results
# ============================================================================


def test_empty_loader_returns_no_skills():
    """When the loader returns nothing, resolve_skills returns ([], [])."""
    skills_mod = _import_skills()

    resolved, skipped = skills_mod.resolve_skills(
        phase="execute",
        allowlist=None,
        _loader=lambda installed_dir: [],
    )
    assert resolved == []
    assert skipped == []


# ============================================================================
# No allowlist (absent key) → all skills returned
# ============================================================================


def test_no_allowlist_returns_all_valid_skills():
    """allowlist=None means no filtering — all valid skills come through."""
    skills_mod = _import_skills()
    loader_skills = [FakeSkill("alpha", "c"), FakeSkill("beta", "c")]

    resolved, skipped = skills_mod.resolve_skills(
        phase="execute",
        allowlist=None,
        _loader=lambda installed_dir: loader_skills,
    )
    assert [s.name for s in resolved] == ["alpha", "beta"]
    assert skipped == []


def test_allowlist_key_absent_returns_all_valid_skills():
    """allowlist present but phase key absent → all skills."""
    skills_mod = _import_skills()
    loader_skills = [FakeSkill("alpha", "c"), FakeSkill("beta", "c")]

    resolved, skipped = skills_mod.resolve_skills(
        phase="execute",
        allowlist={"review": ["alpha"]},  # key for a *different* phase
        _loader=lambda installed_dir: loader_skills,
    )
    assert [s.name for s in resolved] == ["alpha", "beta"]
    assert skipped == []


# ============================================================================
# Allowlist = [] → no skills
# ============================================================================


def test_empty_allowlist_returns_no_skills():
    """allowlist[phase]=[] means zero skills for this phase."""
    skills_mod = _import_skills()
    loader_skills = [FakeSkill("alpha", "c"), FakeSkill("beta", "c")]

    resolved, skipped = skills_mod.resolve_skills(
        phase="execute",
        allowlist={"execute": []},
        _loader=lambda installed_dir: loader_skills,
    )
    assert resolved == []
    # When the allowlist explicitly allows nothing, existing skills are not
    # reported as "skipped" — they simply aren't in scope.
    assert skipped == []


# ============================================================================
# Allowlist = explicit names → exact match
# ============================================================================


def test_explicit_allowlist_returns_named_skills_only():
    """allowlist[phase]=['alpha'] returns only alpha; beta is skipped+reported."""
    skills_mod = _import_skills()
    loader_skills = [FakeSkill("alpha", "c"), FakeSkill("beta", "c")]

    resolved, skipped = skills_mod.resolve_skills(
        phase="execute",
        allowlist={"execute": ["alpha"]},
        _loader=lambda installed_dir: loader_skills,
    )
    assert [s.name for s in resolved] == ["alpha"]
    assert any(s["name"] == "beta" for s in skipped)


def test_allowlist_name_not_installed_is_ignored():
    """A name in the allowlist that isn't installed is silently ignored (not
    an error — the skill just isn't there)."""
    skills_mod = _import_skills()
    loader_skills = [FakeSkill("alpha", "c")]

    resolved, skipped = skills_mod.resolve_skills(
        phase="execute",
        allowlist={"execute": ["alpha", "missing-skill"]},
        _loader=lambda installed_dir: loader_skills,
    )
    assert [s.name for s in resolved] == ["alpha"]
    assert skipped == []  # beta is not installed — nothing to skip


# ============================================================================
# Malformed / un-named skills are skipped and reported
# ============================================================================


def test_unnamed_skill_is_skipped_and_reported():
    """A skill with an empty name (e.g. injected programmatically) is skipped
    and reported.  The real openhands-sdk loader always assigns the directory
    name so empty-name entries won't arise from disk, but resolve_skills guards
    against any caller-provided or future-loader object with a missing name."""
    skills_mod = _import_skills()
    bad = FakeSkill("", "content")
    good = FakeSkill("good-skill", "content")
    loader_skills = [bad, good]

    resolved, skipped = skills_mod.resolve_skills(
        phase="execute",
        allowlist=None,
        _loader=lambda installed_dir: loader_skills,
    )
    assert [s.name for s in resolved] == ["good-skill"]
    # skipped report should mention the malformed entry
    assert len(skipped) == 1
    assert "malformed" in skipped[0]["reason"].lower()


def test_multiple_malformed_skills_all_skipped():
    """All malformed skills are collected in the skipped list."""
    skills_mod = _import_skills()
    loader_skills = [
        FakeSkill("", "content"),
        FakeSkill("", "content"),
        FakeSkill("good", "content"),
    ]

    resolved, skipped = skills_mod.resolve_skills(
        phase="execute",
        allowlist=None,
        _loader=lambda installed_dir: loader_skills,
    )
    assert [s.name for s in resolved] == ["good"]
    assert len(skipped) == 2


# ============================================================================
# AGENT_SKILLS_DIR env override
# ============================================================================


def test_default_dir_is_convergence_path(monkeypatch, tmp_path):
    """Without AGENT_SKILLS_DIR, the loader receives the convergence dir."""
    skills_mod = _import_skills()
    monkeypatch.delenv("AGENT_SKILLS_DIR", raising=False)

    captured: list[Path] = []

    def recording_loader(installed_dir: Path):
        captured.append(installed_dir)
        return []

    skills_mod.resolve_skills(
        phase="execute",
        allowlist=None,
        _loader=recording_loader,
    )

    assert len(captured) == 1
    assert captured[0].as_posix() == "/usr/local/share/agent-skills/installed"


def test_agent_skills_dir_env_overrides_default(monkeypatch, tmp_path):
    """AGENT_SKILLS_DIR env var overrides the default convergence directory."""
    skills_mod = _import_skills()
    custom_dir = str(tmp_path / "my-skills")
    monkeypatch.setenv("AGENT_SKILLS_DIR", custom_dir)

    captured: list[Path] = []

    def recording_loader(installed_dir: Path):
        captured.append(installed_dir)
        return []

    skills_mod.resolve_skills(
        phase="execute",
        allowlist=None,
        _loader=recording_loader,
    )

    assert len(captured) == 1
    assert str(captured[0]) == custom_dir


# ============================================================================
# Filesystem tests: real SKILL.md files → load_installed_skills → resolve_skills
# ============================================================================


def test_resolve_skills_loads_real_skill_from_disk(monkeypatch, tmp_path):
    """A valid SKILL.md on disk is loaded by the real SDK loader and
    returned by resolve_skills."""
    skills_mod = _import_skills()
    monkeypatch.delenv("AGENT_SKILLS_DIR", raising=False)

    installed_dir = tmp_path / "installed"
    installed_dir.mkdir()
    _write_skill(installed_dir, "filesystem-skill")

    monkeypatch.setenv("AGENT_SKILLS_DIR", str(installed_dir))

    resolved, skipped = skills_mod.resolve_skills(phase="execute", allowlist=None)

    assert len(resolved) == 1
    assert resolved[0].name == "filesystem-skill"
    assert skipped == []


def test_resolve_skills_loads_skill_without_frontmatter(monkeypatch, tmp_path):
    """The openhands-sdk 1.24.0 loader normalizes SKILL.md files that lack
    frontmatter by using the directory name as the skill name. resolve_skills
    trusts the SDK loader and returns the skill (with description=None).

    Malformed skills with empty names are caught by resolve_skills' guard
    (tested via injected _loader in test_unnamed_skill_is_skipped_and_reported).
    """
    skills_mod = _import_skills()
    monkeypatch.delenv("AGENT_SKILLS_DIR", raising=False)

    installed_dir = tmp_path / "installed"
    installed_dir.mkdir()
    _write_skill(installed_dir, "good-skill")
    # Write a skill without YAML frontmatter — SDK normalises the name from
    # the directory name and sets description=None.
    no_frontmatter_dir = installed_dir / "no-frontmatter"
    no_frontmatter_dir.mkdir()
    (no_frontmatter_dir / "SKILL.md").write_text("Just some text without frontmatter\n")

    monkeypatch.setenv("AGENT_SKILLS_DIR", str(installed_dir))

    resolved, skipped = skills_mod.resolve_skills(phase="execute", allowlist=None)

    resolved_names = [s.name for s in resolved]
    assert "good-skill" in resolved_names
    assert "no-frontmatter" in resolved_names
    assert len(resolved) == 2


def test_resolve_skills_empty_directory_returns_no_skills(monkeypatch, tmp_path):
    """An empty convergence directory yields no skills and no skipped report."""
    skills_mod = _import_skills()
    monkeypatch.delenv("AGENT_SKILLS_DIR", raising=False)

    installed_dir = tmp_path / "installed"
    installed_dir.mkdir()

    monkeypatch.setenv("AGENT_SKILLS_DIR", str(installed_dir))

    resolved, skipped = skills_mod.resolve_skills(phase="execute", allowlist=None)

    assert resolved == []
    assert skipped == []


def test_resolve_skills_multiple_skills_from_disk(monkeypatch, tmp_path):
    """Multiple valid SKILL.md files are all loaded and returned."""
    skills_mod = _import_skills()
    monkeypatch.delenv("AGENT_SKILLS_DIR", raising=False)

    installed_dir = tmp_path / "installed"
    installed_dir.mkdir()
    _write_skill(installed_dir, "skill-alpha")
    _write_skill(installed_dir, "skill-beta")

    monkeypatch.setenv("AGENT_SKILLS_DIR", str(installed_dir))

    resolved, skipped = skills_mod.resolve_skills(phase="execute", allowlist=None)

    assert len(resolved) == 2
    resolved_names = [s.name for s in resolved]
    assert "skill-alpha" in resolved_names
    assert "skill-beta" in resolved_names
    assert skipped == []
