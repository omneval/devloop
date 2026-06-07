"""Tests for project test-suite discovery and execution (issue #35).

Tests the new ``run_project_tests`` function and how ``handle_execute`` /
``handle_merge`` consume the real pass/fail signal.

All subprocess calls are mocked — no real toolchain runs.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import entrypoint
from entrypoint import AgentOutcome, TaskSpec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _noop_tracer():
    from contextlib import nullcontext

    class _T:
        def start_as_current_span(self, *a, **k):
            return nullcontext()

    return _T()


def _spec(**kw) -> TaskSpec:
    defaults = dict(
        phase="execute",
        project_id="omneval",
        issue_number=42,
        title="Feature X",
        body="do it",
        instructions="go",
        branch="",
        extra={},
    )
    defaults.update(kw)
    return TaskSpec(**defaults)


def _fake_completed(returncode=0, stdout="", stderr=""):
    """Return a CompletedProcess-like object that subprocess.run returns."""
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def origin(tmp_path):
    """A bare 'remote' repo with one commit on main."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git("init", "--bare", "-b", "main", cwd=bare)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-b", "main", cwd=seed)
    _git("config", "user.email", "t@t.com", cwd=seed)
    _git("config", "user.name", "t", cwd=seed)
    (seed / "README.md").write_text("hello\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "init", cwd=seed)
    _git("remote", "add", "origin", str(bare), cwd=seed)
    _git("push", "origin", "main", cwd=seed)
    return bare


# ===========================================================================
# Cycle 1 — no test files present → pass (no tests detected)
# ===========================================================================


def test_no_tests_detected_returns_true(tmp_path):
    """When no recognised project files exist, run_project_tests returns True
    (policy: no tests = pass with a note, so a bare project is not blocked)."""
    passed, output = entrypoint.run_project_tests(str(tmp_path))
    assert passed is True
    # The output should mention that no tests were found
    assert "no test" in output.lower() or output == "" or "skip" in output.lower()


# ===========================================================================
# Cycle 2 — Go ecosystem
# ===========================================================================


def test_go_project_runs_go_test(tmp_path):
    """go.mod present → runs 'go test ./...'."""
    (tmp_path / "go.mod").write_text("module example.com/mymod\ngo 1.21\n")

    with patch(
        "subprocess.run", return_value=_fake_completed(0, "ok  example.com/mymod")
    ) as mock_run:
        passed, output = entrypoint.run_project_tests(str(tmp_path))

    assert passed is True
    # verify 'go test ./...' was called
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert any(c[:3] == ["go", "test", "./..."] for c in calls)


def test_go_test_failure_returns_false(tmp_path):
    """Non-zero exit from 'go test ./...' → passed=False."""
    (tmp_path / "go.mod").write_text("module example.com/mymod\ngo 1.21\n")

    with patch(
        "subprocess.run", return_value=_fake_completed(1, "", "FAIL example.com/mymod")
    ):
        passed, output = entrypoint.run_project_tests(str(tmp_path))

    assert passed is False
    assert "FAIL" in output


# ===========================================================================
# Cycle 3 — Python ecosystem
# ===========================================================================


def test_python_project_runs_pytest(tmp_path):
    """pyproject.toml present → runs pytest."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "myapp"\n')

    with patch(
        "subprocess.run", return_value=_fake_completed(0, "1 passed")
    ) as mock_run:
        passed, output = entrypoint.run_project_tests(str(tmp_path))

    assert passed is True
    calls = [c.args[0] for c in mock_run.call_args_list]
    # should call pytest or python -m pytest
    assert any("pytest" in " ".join(c) for c in calls)


def test_setup_py_project_runs_pytest(tmp_path):
    """setup.py present → also detects Python and runs pytest."""
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup; setup(name='x')\n"
    )

    with patch(
        "subprocess.run", return_value=_fake_completed(0, "1 passed")
    ) as mock_run:
        passed, output = entrypoint.run_project_tests(str(tmp_path))

    assert passed is True
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert any("pytest" in " ".join(c) for c in calls)


def test_python_test_failure_returns_false(tmp_path):
    """Non-zero exit from pytest → passed=False."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "myapp"\n')

    with patch("subprocess.run", return_value=_fake_completed(1, "", "1 failed")):
        passed, output = entrypoint.run_project_tests(str(tmp_path))

    assert passed is False


# ===========================================================================
# Cycle 4 — Node ecosystem with a real test script
# ===========================================================================


def test_node_with_real_test_script_runs_npm_test(tmp_path):
    """package.json with a real test script → runs 'npm test'."""
    pkg = {
        "name": "myapp",
        "scripts": {"test": "jest --ci"},
    }
    (tmp_path / "package.json").write_text(json.dumps(pkg))

    with patch(
        "subprocess.run", return_value=_fake_completed(0, "Tests: 5 passed")
    ) as mock_run:
        passed, output = entrypoint.run_project_tests(str(tmp_path))

    assert passed is True
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert any(c[:2] == ["npm", "test"] for c in calls)


def test_node_test_failure_returns_false(tmp_path):
    """Non-zero npm test → passed=False."""
    pkg = {"name": "myapp", "scripts": {"test": "jest --ci"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))

    with patch(
        "subprocess.run", return_value=_fake_completed(1, "", "Tests: 2 failed")
    ):
        passed, output = entrypoint.run_project_tests(str(tmp_path))

    assert passed is False


# ===========================================================================
# Cycle 5 — Node with npm default placeholder → skip (no false-fail)
# ===========================================================================


def test_node_npm_default_placeholder_is_skipped(tmp_path):
    """package.json with the npm default 'echo ... && exit 1' placeholder
    must NOT cause a false test failure — it is treated as 'no tests'."""
    pkg = {
        "name": "myapp",
        "scripts": {"test": 'echo "Error: no test specified" && exit 1'},
    }
    (tmp_path / "package.json").write_text(json.dumps(pkg))

    with patch("subprocess.run") as mock_run:
        passed, output = entrypoint.run_project_tests(str(tmp_path))

    # npm test should NOT have been invoked
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert not any(c[:2] == ["npm", "test"] for c in calls)
    assert passed is True


def test_node_no_test_script_is_skipped(tmp_path):
    """package.json with no 'test' key in scripts is treated as no tests."""
    pkg = {"name": "myapp", "scripts": {"build": "tsc"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))

    with patch("subprocess.run") as mock_run:
        passed, output = entrypoint.run_project_tests(str(tmp_path))

    calls = [c.args[0] for c in mock_run.call_args_list]
    assert not any(c[:2] == ["npm", "test"] for c in calls)
    assert passed is True


def test_node_missing_scripts_key_is_skipped(tmp_path):
    """package.json with no 'scripts' section is treated as no tests."""
    pkg = {"name": "myapp"}
    (tmp_path / "package.json").write_text(json.dumps(pkg))

    with patch("subprocess.run") as mock_run:
        passed, output = entrypoint.run_project_tests(str(tmp_path))

    calls = [c.args[0] for c in mock_run.call_args_list]
    assert not any(c[:2] == ["npm", "test"] for c in calls)
    assert passed is True


# ===========================================================================
# Cycle 6 — Multi-ecosystem (Go + Python): all must pass
# ===========================================================================


def test_multi_ecosystem_runs_all_suites(tmp_path):
    """go.mod + pyproject.toml → both 'go test ./...' and pytest are run."""
    (tmp_path / "go.mod").write_text("module example.com/m\ngo 1.21\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')

    with patch("subprocess.run", return_value=_fake_completed(0, "ok")) as mock_run:
        passed, output = entrypoint.run_project_tests(str(tmp_path))

    assert passed is True
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert any(c[:3] == ["go", "test", "./..."] for c in calls)
    assert any("pytest" in " ".join(c) for c in calls)


def test_multi_ecosystem_one_fails_returns_false(tmp_path):
    """If any suite fails, run_project_tests returns False."""
    (tmp_path / "go.mod").write_text("module example.com/m\ngo 1.21\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')

    call_count = [0]

    def side_effect(cmd, *args, **kwargs):
        call_count[0] += 1
        # Go passes, Python fails
        if cmd[0] == "go":
            return _fake_completed(0, "ok")
        else:
            return _fake_completed(1, "", "1 failed")

    with patch("subprocess.run", side_effect=side_effect):
        passed, output = entrypoint.run_project_tests(str(tmp_path))

    assert passed is False
    assert call_count[0] >= 2  # both suites ran


# ===========================================================================
# Cycle 7 — Timeout is honoured
# ===========================================================================


def test_run_project_tests_passes_timeout(tmp_path):
    """run_project_tests passes a timeout to subprocess so a hung suite
    does not hang the Job forever."""
    (tmp_path / "go.mod").write_text("module example.com/m\ngo 1.21\n")

    with patch("subprocess.run", return_value=_fake_completed(0, "ok")) as mock_run:
        entrypoint.run_project_tests(str(tmp_path), timeout=120)

    # At least one subprocess.run call must have a 'timeout' kwarg
    assert any(c.kwargs.get("timeout") for c in mock_run.call_args_list)


# ===========================================================================
# Cycle 8 — handle_execute uses real tests_passed
# ===========================================================================


def test_handle_execute_tests_passed_true_when_green(origin, tmp_path, monkeypatch):
    """handle_execute sets tests_passed=True when the test suite passes."""
    workdir = tmp_path / "repo"

    monkeypatch.setattr(
        entrypoint,
        "run_agent",
        lambda spec, wd, tracer: AgentOutcome(summary="done", files_changed=True),
    )
    monkeypatch.setattr(
        entrypoint, "open_draft_pr", lambda *a, **k: "https://github.com/x/y/pull/1"
    )
    monkeypatch.setattr(
        entrypoint, "run_project_tests", lambda wd, **kw: (True, "1 passed")
    )
    monkeypatch.setattr(entrypoint, "_commit_count", lambda *a, **k: 1)

    monkeypatch.setenv("GITHUB_URL", str(origin))
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    monkeypatch.setenv("WORKDIR", str(workdir))

    spec = _spec(issue_number=42, title="feat")
    result = entrypoint.handle_execute(spec, _noop_tracer())

    assert result["tests_passed"] is True


def test_handle_execute_tests_passed_false_when_red(origin, tmp_path, monkeypatch):
    """handle_execute sets tests_passed=False when the test suite fails."""
    workdir = tmp_path / "repo"

    monkeypatch.setattr(
        entrypoint,
        "run_agent",
        lambda spec, wd, tracer: AgentOutcome(summary="done", files_changed=True),
    )
    monkeypatch.setattr(
        entrypoint, "open_draft_pr", lambda *a, **k: "https://github.com/x/y/pull/1"
    )
    monkeypatch.setattr(
        entrypoint,
        "run_project_tests",
        lambda wd, **kw: (False, "FAIL: assertion error"),
    )
    monkeypatch.setattr(entrypoint, "_commit_count", lambda *a, **k: 1)

    monkeypatch.setenv("GITHUB_URL", str(origin))
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    monkeypatch.setenv("WORKDIR", str(workdir))

    spec = _spec(issue_number=42, title="feat")
    result = entrypoint.handle_execute(spec, _noop_tracer())

    assert result["tests_passed"] is False


def test_handle_execute_includes_test_output_in_summary_on_failure(
    origin, tmp_path, monkeypatch
):
    """handle_execute includes test output in summary field so failures are visible."""
    workdir = tmp_path / "repo"

    monkeypatch.setattr(
        entrypoint,
        "run_agent",
        lambda spec, wd, tracer: AgentOutcome(summary="done", files_changed=True),
    )
    monkeypatch.setattr(
        entrypoint, "open_draft_pr", lambda *a, **k: "https://github.com/x/y/pull/1"
    )
    monkeypatch.setattr(
        entrypoint,
        "run_project_tests",
        lambda wd, **kw: (False, "FAIL: assertion x != y"),
    )
    monkeypatch.setattr(entrypoint, "_commit_count", lambda *a, **k: 1)

    monkeypatch.setenv("GITHUB_URL", str(origin))
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    monkeypatch.setenv("WORKDIR", str(workdir))

    spec = _spec(issue_number=42, title="feat")
    result = entrypoint.handle_execute(spec, _noop_tracer())

    # The summary must contain something from the test failure output
    assert (
        "FAIL" in result.get("summary", "")
        or "assertion" in result.get("summary", "").lower()
    )


def test_handle_execute_still_opens_pr_even_on_red(origin, tmp_path, monkeypatch):
    """handle_execute opens the draft PR even when tests fail (it's a draft)."""
    workdir = tmp_path / "repo"
    pr_opened = []

    monkeypatch.setattr(
        entrypoint,
        "run_agent",
        lambda spec, wd, tracer: AgentOutcome(summary="done", files_changed=True),
    )
    monkeypatch.setattr(
        entrypoint,
        "open_draft_pr",
        lambda *a, **k: pr_opened.append(True) or "https://github.com/x/y/pull/1",
    )
    monkeypatch.setattr(
        entrypoint, "run_project_tests", lambda wd, **kw: (False, "1 failed")
    )
    monkeypatch.setattr(entrypoint, "_commit_count", lambda *a, **k: 1)

    monkeypatch.setenv("GITHUB_URL", str(origin))
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    monkeypatch.setenv("WORKDIR", str(workdir))

    spec = _spec(issue_number=42, title="feat")
    entrypoint.handle_execute(spec, _noop_tracer())

    assert pr_opened, "draft PR must still be opened even when tests fail"


# ===========================================================================
# Cycle 9 — handle_merge opens a review PR (PR-review model)
#
# The merge phase no longer merges into the default branch or re-runs the
# suite; it opens a ready-for-review PR for each approved branch and tags the
# reviewer. The human reviews + merges on GitHub (the PR's `Closes #N` closes
# the issue). These tests pin that contract; gh-call details live in
# test_run_agent.py's open_review_pr tests.
# ===========================================================================


def _merge_env(origin, monkeypatch, reviewer="zbloss"):
    monkeypatch.setenv("GITHUB_URL", str(origin))
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    monkeypatch.setenv("PR_REVIEWER", reviewer)


def test_handle_merge_opens_pr_and_never_pushes_main(origin, monkeypatch):
    """handle_merge opens a review PR for the branch and must NOT push the
    default branch."""
    _merge_env(origin, monkeypatch)
    opened = []
    monkeypatch.setattr(
        entrypoint,
        "open_review_pr",
        lambda *a, **k: opened.append(a) or "https://x/pull/1",
    )
    monkeypatch.setattr(
        entrypoint,
        "push_branch",
        lambda *a, **k: pytest.fail("merge must not push main"),
    )

    spec = _spec(
        phase="merge",
        extra={"branches": ["agent/issue-1"], "issues": [{"id": "1", "title": "X"}]},
    )
    result = entrypoint.handle_merge(spec, _noop_tracer())

    assert opened, "open_review_pr must be called"
    assert result["status"] == "complete"
    assert result["pr_url"] == "https://x/pull/1"


def test_handle_merge_fails_with_info_when_no_pr(origin, monkeypatch):
    """If no PR could be opened, the phase fails and carries failure info for
    notification rather than silently succeeding."""
    _merge_env(origin, monkeypatch)
    monkeypatch.setattr(entrypoint, "open_review_pr", lambda *a, **k: "")

    spec = _spec(
        phase="merge",
        extra={"branches": ["agent/issue-1"], "issues": [{"id": "1", "title": "X"}]},
    )
    result = entrypoint.handle_merge(spec, _noop_tracer())

    assert result["status"] == "failed"
    assert bool(result.get("error")) or bool(result.get("summary"))


def test_handle_merge_sets_merged_issues_on_success(origin, monkeypatch):
    """Successful merge result includes the issue list (issues whose PRs were
    opened for review)."""
    _merge_env(origin, monkeypatch)
    monkeypatch.setattr(
        entrypoint, "open_review_pr", lambda *a, **k: "https://x/pull/7"
    )

    spec = _spec(
        phase="merge",
        extra={
            "branches": ["agent/issue-7"],
            "issues": [{"id": "7", "title": "Seven"}],
        },
    )
    result = entrypoint.handle_merge(spec, _noop_tracer())

    assert result.get("merged_issues") == [7]
