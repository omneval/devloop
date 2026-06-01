"""Integration test for the Agent Execution Job entrypoint (issue #19).

No cluster: a local bare repo stands in for the GitHub remote, ``run_agent`` is
mocked to make a file change, ``open_draft_pr`` is stubbed (no gh auth), and the
output sink writes to a local file (OUTPUT_FILE) instead of a ConfigMap.
"""

import json
import subprocess
from pathlib import Path

import pytest

import entrypoint


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


def test_execute_pushes_branch_and_writes_output(origin, tmp_path, monkeypatch):
    workdir = tmp_path / "repo"
    out_file = tmp_path / "out.json"

    def fake_run_agent(spec, wd, tracer):
        # simulate the agent editing a file
        Path(wd, "feature.txt").write_text("implemented\n")
        return entrypoint.AgentOutcome(summary="did the thing", files_changed=True)

    monkeypatch.setattr(entrypoint, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        entrypoint, "open_draft_pr",
        lambda *a, **k: "https://github.com/omneval/omneval/pull/5",
    )

    monkeypatch.setenv("TASK_SPEC", json.dumps({
        "phase": "execute", "project_id": "omneval", "issue_number": 5,
        "title": "Add feature", "body": "do it", "instructions": "go",
    }))
    monkeypatch.setenv("GITHUB_URL", str(origin))
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    monkeypatch.setenv("WORKDIR", str(workdir))
    monkeypatch.setenv("OUTPUT_CONFIGMAP", "agent-omneval-execute-5-a1")
    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    rc = entrypoint.main()
    assert rc == 0

    # the branch was pushed to origin
    branches = subprocess.run(
        ["git", "branch", "--list", "agent/issue-5"], cwd=origin,
        capture_output=True, text=True,
    ).stdout
    assert "agent/issue-5" in branches

    # output payload written
    payload = json.loads(out_file.read_text())
    assert payload["status"] == "complete"
    assert payload["branch"] == "agent/issue-5"
    assert payload["pr_url"].endswith("/pull/5")


def test_plan_phase_parses_plan_block(origin, tmp_path, monkeypatch):
    """Plan phase clones, runs the planner, and returns the <plan> it emits."""
    workdir = tmp_path / "repo"
    out_file = tmp_path / "out.json"

    plan_json = '{"issues": [{"id": "1", "title": "First", "branch": "agent/issue-1"}]}'

    def fake_run_agent(spec, wd, tracer):
        return entrypoint.AgentOutcome(
            summary=f"Here is the plan.\n<plan>\n{plan_json}\n</plan>\n",
        )

    monkeypatch.setattr(entrypoint, "run_agent", fake_run_agent)
    monkeypatch.setenv("TASK_SPEC", json.dumps({
        "phase": "plan", "project_id": "omneval",
        "extra": {"agent_label": "agent-ready", "feedback": ""},
    }))
    monkeypatch.setenv("GITHUB_URL", str(origin))
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    monkeypatch.setenv("WORKDIR", str(workdir))
    monkeypatch.setenv("OUTPUT_CONFIGMAP", "agent-omneval-plan-a1")
    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    assert entrypoint.main() == 0
    payload = json.loads(out_file.read_text())
    assert payload["status"] == "complete"
    assert payload["plan"]["issues"][0]["branch"] == "agent/issue-1"


def test_build_agent_message_renders_bundled_prompt(monkeypatch):
    """The implement prompt is loaded from the bundled templates and the
    {{TASK_ID}}/{{BRANCH}} placeholders are substituted (none left as literals)."""
    prompts_dir = Path(__file__).parent / "prompts"
    monkeypatch.setenv("AGENT_PROMPTS_DIR", str(prompts_dir))
    spec = entrypoint.TaskSpec(
        phase="execute", project_id="omneval", issue_number=42,
        title="Fix auth", branch="agent/issue-42",
    )
    msg = entrypoint.build_agent_message(spec)
    assert "Fix issue 42: Fix auth" in msg
    assert "agent/issue-42" in msg
    assert "{{" not in msg  # every placeholder substituted or stripped


def test_extract_plan_handles_missing_block():
    assert entrypoint._extract_plan("no plan here") is None
    assert entrypoint._extract_plan("<plan>{\"issues\": []}</plan>") == {"issues": []}


def test_unknown_phase_writes_failure(tmp_path, monkeypatch):
    out_file = tmp_path / "out.json"
    monkeypatch.setenv("TASK_SPEC", json.dumps({"phase": "bogus", "project_id": "x"}))
    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    assert entrypoint.main() == 1
    assert json.loads(out_file.read_text())["status"] == "failed"


def test_stub_mode_round_trip(origin, tmp_path, monkeypatch):
    """AGENT_STUB=1 proves the dispatch→ConfigMap round-trip (issue #18)."""
    workdir = tmp_path / "repo"
    out_file = tmp_path / "out.json"
    monkeypatch.setattr(entrypoint, "open_draft_pr", lambda *a, **k: "pr://stub")
    monkeypatch.setenv("AGENT_STUB", "1")
    monkeypatch.setenv("TASK_SPEC", json.dumps({
        "phase": "execute", "project_id": "omneval", "issue_number": 7, "title": "x",
    }))
    monkeypatch.setenv("GITHUB_URL", str(origin))
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    monkeypatch.setenv("WORKDIR", str(workdir))
    monkeypatch.setenv("OUTPUT_CONFIGMAP", "j7")
    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    assert entrypoint.main() == 0
    assert json.loads(out_file.read_text())["status"] == "complete"
