"""Tests for default agent skills and skill resolution (issue #20).

Verifies that:
1. Default skills are loaded from the baked-in directory.
2. Project-level skills override baked-in defaults by name.
3. A project skill with empty content removes the default skill.
4. resolve_effective_skills returns the merged skill map.
5. list_skills returns skill names from a directory.
6. Skills can be installed via npx skills CLI (mocked).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skills import (
    DEFAULT_SKILLS_DIR,
    list_skills,
    resolve_effective_skills,
    install_skills,
)


class TestListSkills:
    """list_skills returns the names of skill directories."""

    def test_empty_directory_returns_empty(self, tmp_path: Path):
        result = list_skills(tmp_path)
        assert result == []

    def test_returns_skill_names(self, tmp_path: Path):
        (tmp_path / "tdd").mkdir()
        (tmp_path / "debugging").mkdir()
        (tmp_path / "architecture-review").mkdir()
        (tmp_path / "not-a-skill.txt").write_text("ignored")

        result = sorted(list_skills(tmp_path))
        assert result == ["architecture-review", "debugging", "tdd"]

    def test_nested_dirs_not_included(self, tmp_path: Path):
        (tmp_path / "tdd").mkdir()
        (tmp_path / "tdd" / "subdir").mkdir()

        result = list_skills(tmp_path)
        assert result == ["tdd"]


class TestResolveEffectiveSkills:
    """resolve_effective_skills merges defaults with project-level overrides."""

    def _setup_skills(
        self,
        tmp_path: Path,
        defaults: dict[str, str] | None = None,
        project: dict[str, str] | None = None,
    ) -> tuple[Path, Path]:
        """Create default and project skill dirs, return (default_dir, project_dir)."""
        default_dir = tmp_path / "defaults"
        project_dir = tmp_path / "project" / ".agents" / "skills"

        if defaults:
            default_dir.mkdir(parents=True, exist_ok=True)
            for name, content in defaults.items():
                skill_dir = default_dir / name
                skill_dir.mkdir(parents=True, exist_ok=True)
                (skill_dir / "SKILL.md").write_text(content)

        if project:
            project_dir.mkdir(parents=True, exist_ok=True)
            for name, content in project.items():
                skill_dir = project_dir / name
                skill_dir.mkdir(parents=True, exist_ok=True)
                (skill_dir / "SKILL.md").write_text(content)

        return default_dir, project_dir

    def test_returns_defaults_when_no_project_skills(self, tmp_path: Path):
        default_dir, _ = self._setup_skills(
            tmp_path,
            defaults={
                "tdd": "default tdd skill",
                "debugging": "default debugging skill",
            },
        )

        result = resolve_effective_skills(tmp_path / "project", default_dir)

        assert sorted(result.keys()) == ["debugging", "tdd"]
        assert "default tdd" in result["tdd"]
        assert "default debugging" in result["debugging"]

    def test_project_skills_override_defaults(self, tmp_path: Path):
        default_dir, project_dir = self._setup_skills(
            tmp_path,
            defaults={"tdd": "default tdd skill"},
            project={"tdd": "custom tdd skill"},
        )

        result = resolve_effective_skills(tmp_path / "project", default_dir)

        assert "tdd" in result
        assert "custom tdd" in result["tdd"]
        assert "default tdd" not in result["tdd"]

    def test_project_can_add_new_skills(self, tmp_path: Path):
        default_dir, project_dir = self._setup_skills(
            tmp_path,
            defaults={"tdd": "default tdd"},
            project={"tdd": "custom tdd", "custom-skill": "my custom skill"},
        )

        result = resolve_effective_skills(tmp_path / "project", default_dir)

        assert "tdd" in result
        assert "custom-skill" in result
        assert "custom tdd" in result["tdd"]
        assert "my custom skill" in result["custom-skill"]

    def test_empty_project_skill_removes_default(self, tmp_path: Path):
        """A project skill dir with no SKILL.md (or empty content) removes the default."""
        default_dir, project_dir = self._setup_skills(
            tmp_path,
            defaults={"tdd": "default tdd skill", "debugging": "default debugging"},
            project={"tdd": ""},
        )

        result = resolve_effective_skills(tmp_path / "project", default_dir)

        assert "tdd" not in result
        assert "debugging" in result

    def test_missing_default_dir_is_ok(self, tmp_path: Path):
        """When default skills dir doesn't exist, only project skills are returned."""
        _, project_dir = self._setup_skills(
            tmp_path,
            defaults=None,  # default dir won't exist
            project={"custom-skill": "my custom skill"},
        )

        result = resolve_effective_skills(
            tmp_path / "project",
            tmp_path / "nonexistent_defaults",
        )

        assert "custom-skill" in result

    def test_no_skills_returns_empty(self, tmp_path: Path):
        result = resolve_effective_skills(
            tmp_path / "project",
            tmp_path / "nonexistent",
        )
        assert result == {}


class TestInstallSkills:
    """install_skills invokes npx skills add for each skill spec."""

    def test_install_runs_npx_command(self, tmp_path: Path, monkeypatch):
        """install_skills calls npx skills add for each skill."""
        target_dir = tmp_path / "skills"
        target_dir.mkdir()

        call_log = []

        def fake_run(cmd, cwd=None):
            call_log.append(cmd)

        monkeypatch.setattr("skills._run", fake_run)

        install_skills(
            target_dir=str(target_dir),
            skills=[
                {"repo": "mattpocock/skills", "skill": "tdd"},
                {"repo": "mattpocock/skills", "skill": "debugging"},
            ],
        )

        assert len(call_log) == 2
        assert "npx" in " ".join(call_log[0])
        assert "skills" in " ".join(call_log[0])
        assert "mattpocock/skills" in " ".join(call_log[0])
        assert "tdd" in " ".join(call_log[0])

    def test_install_with_agent_flag(self, tmp_path: Path, monkeypatch):
        """install_skills passes --agent=openhands flag."""
        target_dir = tmp_path / "skills"
        target_dir.mkdir()

        call_log = []

        def fake_run(cmd, cwd=None):
            call_log.append(cmd)

        monkeypatch.setattr("skills._run", fake_run)

        install_skills(
            target_dir=str(target_dir),
            skills=[{"repo": "mattpocock/skills", "skill": "tdd"}],
            agent="openhands",
        )

        cmd_str = " ".join(call_log[0])
        assert "-a" in call_log[0] or "--agent" in call_log[0]
        assert "openhands" in cmd_str

    def test_install_no_skills_is_noop(self, tmp_path: Path, monkeypatch):
        """Empty skill list produces no npx calls."""
        target_dir = tmp_path / "skills"
        target_dir.mkdir()

        call_log = []

        def fake_run(cmd, cwd=None):
            call_log.append(cmd)

        monkeypatch.setattr("skills._run", fake_run)

        install_skills(target_dir=str(target_dir), skills=[])

        assert call_log == []


class TestDefaultSkillsDir:
    """DEFAULT_SKILLS_DIR constant is set correctly."""

    def test_default_skills_dir_is_set(self):
        assert DEFAULT_SKILLS_DIR == "/usr/local/share/agent-skills"
        assert isinstance(DEFAULT_SKILLS_DIR, str)
