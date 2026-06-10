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


# --------------------------------------------------------------------------- #
# install_configmap_skills tests (issue #34)
# --------------------------------------------------------------------------- #


def test_install_configmap_skills_copies_staging_to_convergence(monkeypatch, tmp_path):
    """Skills staged as <name>/SKILL.md (K8s ConfigMap mount layout) are installed."""
    skills_mod = _import_skills()
    monkeypatch.delenv("AGENT_SKILLS_DIR", raising=False)

    staging = tmp_path / "staging"
    staging.mkdir()
    convergence = tmp_path / "convergence"

    # K8s mounts ConfigMap keys with '/' as <name>/SKILL.md subdirectories.
    (staging / "deploy-review").mkdir()
    (staging / "deploy-review" / "SKILL.md").write_text(
        "# Deploy Review Skill\nContent here.\n"
    )
    (staging / "code-refactor").mkdir()
    (staging / "code-refactor" / "SKILL.md").write_text(
        "# Code Refactor Skill\nAnother skill.\n"
    )

    monkeypatch.setenv("AGENT_SKILLS_DIR", str(convergence))

    installed = skills_mod.install_configmap_skills(str(staging))

    assert sorted(installed) == ["code-refactor", "deploy-review"]
    assert (
        convergence / "deploy-review" / "SKILL.md"
    ).read_text() == "# Deploy Review Skill\nContent here.\n"
    assert (
        convergence / "code-refactor" / "SKILL.md"
    ).read_text() == "# Code Refactor Skill\nAnother skill.\n"


def test_install_configmap_skills_overrides_baked_skill_on_collision(
    monkeypatch, tmp_path
):
    """A ConfigMap skill with the same name as a baked skill overwrites it."""
    skills_mod = _import_skills()
    monkeypatch.delenv("AGENT_SKILLS_DIR", raising=False)

    staging = tmp_path / "staging"
    staging.mkdir()
    convergence = tmp_path / "convergence"

    # Pre-seed a baked skill
    baked_dir = convergence / "deploy-review"
    baked_dir.mkdir(parents=True)
    (baked_dir / "SKILL.md").write_text("# Baked Deploy Review\nOriginal content.\n")

    # ConfigMap delivers a skill with the same name (K8s subdirectory layout)
    (staging / "deploy-review").mkdir()
    (staging / "deploy-review" / "SKILL.md").write_text(
        "# ConfigMap Deploy Review\nOverridden content.\n"
    )

    monkeypatch.setenv("AGENT_SKILLS_DIR", str(convergence))

    installed = skills_mod.install_configmap_skills(str(staging))

    assert installed == ["deploy-review"]
    assert (
        convergence / "deploy-review" / "SKILL.md"
    ).read_text() == "# ConfigMap Deploy Review\nOverridden content.\n"


def test_install_configmap_skills_skips_bad_skill_and_installs_others(
    monkeypatch, tmp_path
):
    """A failure writing one skill is logged and skipped; other skills install fine.

    We patch shutil.copy2 to raise OSError for the bad-skill so the test is
    deterministic regardless of the host user (root bypasses permission checks).
    """
    import shutil as _shutil
    import unittest.mock

    skills_mod = _import_skills()
    monkeypatch.delenv("AGENT_SKILLS_DIR", raising=False)

    staging = tmp_path / "staging"
    staging.mkdir()
    convergence = tmp_path / "convergence"

    (staging / "good-skill").mkdir()
    (staging / "good-skill" / "SKILL.md").write_text("# Good\n")
    (staging / "bad-skill").mkdir()
    (staging / "bad-skill" / "SKILL.md").write_text("# Bad\n")

    monkeypatch.setenv("AGENT_SKILLS_DIR", str(convergence))

    original_copy2 = _shutil.copy2

    def flaky_copy2(src, dst):
        if "bad-skill" in str(dst):
            raise OSError("simulated write failure")
        return original_copy2(src, dst)

    with unittest.mock.patch.object(_shutil, "copy2", side_effect=flaky_copy2):
        installed = skills_mod.install_configmap_skills(str(staging))

    assert "good-skill" in installed
    assert "bad-skill" not in installed


def test_install_configmap_skills_returns_empty_when_staging_missing(
    monkeypatch, tmp_path
):
    """When the staging path doesn't exist, no skills are installed."""
    skills_mod = _import_skills()
    monkeypatch.delenv("AGENT_SKILLS_DIR", raising=False)

    installed = skills_mod.install_configmap_skills(str(tmp_path / "nonexistent"))

    assert installed == []


def test_install_configmap_skills_skips_subdirs_without_skill_md(monkeypatch, tmp_path):
    """Subdirectories without a SKILL.md file are skipped."""
    skills_mod = _import_skills()
    monkeypatch.delenv("AGENT_SKILLS_DIR", raising=False)

    staging = tmp_path / "staging"
    staging.mkdir()
    convergence = tmp_path / "convergence"

    (staging / "my-skill").mkdir()
    (staging / "my-skill" / "SKILL.md").write_text("# Skill\n")
    (staging / "empty-dir").mkdir()  # no SKILL.md — should be skipped

    monkeypatch.setenv("AGENT_SKILLS_DIR", str(convergence))

    installed = skills_mod.install_configmap_skills(str(staging))

    assert installed == ["my-skill"]
    assert (convergence / "my-skill" / "SKILL.md").exists()


# ---------------------------------------------------------------------------
# install_repo_skills — repo-native .devloop/skills/ delivery
# ---------------------------------------------------------------------------


class TestInstallRepoSkills:
    def _repo(self, tmp_path, name="my-skill", files=None):
        skill_dir = tmp_path / "repo" / ".devloop" / "skills" / name
        skill_dir.mkdir(parents=True)
        for rel, content in (
            files or {"SKILL.md": "---\nname: my-skill\n---\nbody"}
        ).items():
            p = skill_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return tmp_path / "repo"

    def test_installs_multi_file_skill_tree(self, tmp_path, monkeypatch):
        skills_mod = _import_skills()
        convergence = tmp_path / "convergence"
        convergence.mkdir()
        monkeypatch.setenv("AGENT_SKILLS_DIR", str(convergence))
        repo = self._repo(
            tmp_path,
            files={
                "SKILL.md": "---\nname: my-skill\n---\nbody",
                "references/deep.md": "reference doc",
            },
        )

        installed = skills_mod.install_repo_skills(str(repo))

        assert installed == ["my-skill"]
        assert (convergence / "my-skill" / "SKILL.md").is_file()
        assert (convergence / "my-skill" / "references" / "deep.md").is_file()

    def test_repo_skill_replaces_existing_convergence_entry(
        self, tmp_path, monkeypatch
    ):
        """Most-specific wins: a repo skill overwrites a baked/ConfigMap skill
        of the same name, including stale extra files."""
        skills_mod = _import_skills()
        convergence = tmp_path / "convergence"
        baked = convergence / "my-skill"
        baked.mkdir(parents=True)
        (baked / "SKILL.md").write_text("baked version")
        (baked / "stale.md").write_text("should disappear")
        monkeypatch.setenv("AGENT_SKILLS_DIR", str(convergence))
        repo = self._repo(tmp_path)

        installed = skills_mod.install_repo_skills(str(repo))

        assert installed == ["my-skill"]
        assert "body" in (convergence / "my-skill" / "SKILL.md").read_text()
        assert not (convergence / "my-skill" / "stale.md").exists()

    def test_no_devloop_skills_dir_is_noop(self, tmp_path, monkeypatch):
        skills_mod = _import_skills()
        convergence = tmp_path / "convergence"
        convergence.mkdir()
        monkeypatch.setenv("AGENT_SKILLS_DIR", str(convergence))
        repo = tmp_path / "repo"
        repo.mkdir()

        assert skills_mod.install_repo_skills(str(repo)) == []
        assert list(convergence.iterdir()) == []

    def test_skips_entries_without_skill_md_and_non_dirs(self, tmp_path, monkeypatch):
        skills_mod = _import_skills()
        convergence = tmp_path / "convergence"
        convergence.mkdir()
        monkeypatch.setenv("AGENT_SKILLS_DIR", str(convergence))
        repo = self._repo(tmp_path, name="good")
        skills_root = repo / ".devloop" / "skills"
        (skills_root / "no-manifest").mkdir()
        (skills_root / "stray-file.md").write_text("not a skill dir")

        installed = skills_mod.install_repo_skills(str(repo))

        assert installed == ["good"]
        assert not (convergence / "no-manifest").exists()
        assert not (convergence / "stray-file.md").exists()
