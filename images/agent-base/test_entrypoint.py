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
        entrypoint,
        "open_draft_pr",
        lambda *a, **k: "https://github.com/omneval/omneval/pull/5",
    )

    monkeypatch.setenv(
        "TASK_SPEC",
        json.dumps(
            {
                "phase": "execute",
                "project_id": "omneval",
                "issue_number": 5,
                "title": "Add feature",
                "body": "do it",
                "instructions": "go",
            }
        ),
    )
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
        ["git", "branch", "--list", "agent/issue-5"],
        cwd=origin,
        capture_output=True,
        text=True,
    ).stdout
    assert "agent/issue-5" in branches

    # output payload written
    payload = json.loads(out_file.read_text())
    assert payload["status"] == "complete"
    assert payload["branch"] == "agent/issue-5"
    assert payload["pr_url"].endswith("/pull/5")


def test_plan_phase_parses_plan_block(origin, tmp_path, monkeypatch):
    """Plan phase clones, runs the planner, and returns the plan via structured_extractor."""
    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.chat.chat_completion import Choice
    from unittest.mock import MagicMock, patch

    workdir = tmp_path / "repo"
    out_file = tmp_path / "out.json"

    plan_json = '{"issues": [{"id": "1", "title": "First", "branch": "agent/issue-1"}]}'

    def fake_run_agent(spec, wd, tracer):
        return entrypoint.AgentOutcome(
            summary=f"Here is the plan.\n{plan_json}\n",
        )

    monkeypatch.setattr(entrypoint, "run_agent", fake_run_agent)
    monkeypatch.setenv("AGENT_MODEL", "test-model")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "fake-key")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", "http://fake")

    mock_response = ChatCompletion(
        id="test",
        created=0,
        model="test",
        object="chat.completion",
        choices=[
            Choice(
                index=0,
                finish_reason="stop",
                message=ChatCompletionMessage(content=plan_json, role="assistant"),
            )
        ],
    )

    with patch.object(entrypoint, "_get_llm_client") as mock_client_fn:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        mock_client_fn.return_value = mock_client

        monkeypatch.setenv(
            "TASK_SPEC",
            json.dumps(
                {
                    "phase": "plan",
                    "project_id": "omneval",
                    "extra": {"agent_label": "agent-ready", "feedback": ""},
                }
            ),
        )
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
        phase="execute",
        project_id="omneval",
        issue_number=42,
        title="Fix auth",
        branch="agent/issue-42",
    )
    msg = entrypoint.build_agent_message(spec)
    assert "Fix issue 42: Fix auth" in msg
    assert "agent/issue-42" in msg
    assert "{{" not in msg  # every placeholder substituted or stripped


def test_build_agent_message_scopes_plan_to_triggering_issue(monkeypatch):
    """The plan prompt must be rendered with the triggering issue's number
    (TaskSpec.issue_number) substituted into {{TRIGGERING_ISSUE}}, so the Plan
    agent fetches and scopes its work to that single issue rather than
    replanning the whole agent-ready backlog (caught in real-cluster E2E
    testing — without this, the agent could pick a different, larger issue to
    execute first, surprising whoever applied the agent-ready label)."""
    prompts_dir = Path(__file__).parent / "prompts"
    monkeypatch.setenv("AGENT_PROMPTS_DIR", str(prompts_dir))
    spec = entrypoint.TaskSpec(
        phase="plan",
        project_id="omneval",
        issue_number=42,
        extra={"agent_label": "agent-ready"},
    )
    msg = entrypoint.build_agent_message(spec)
    assert "gh issue view 42" in msg
    assert "{{" not in msg


def test_remediation_prompt_renders_with_ci_check_failures(monkeypatch):
    """The remediation prompt is loaded from the bundled template and the
    {{BRANCH}}/{{CI_CHECK_FAILURES}} placeholders are substituted."""
    prompts_dir = Path(__file__).parent / "prompts"
    monkeypatch.setenv("AGENT_PROMPTS_DIR", str(prompts_dir))
    spec = entrypoint.TaskSpec(
        phase="remediation",
        project_id="omneval",
        issue_number=42,
        branch="agent/issue-42",
        extra={
            "ci_check_failures": "test-unit: exit code 1\nlint: exit code 2",
        },
    )
    msg = entrypoint.build_agent_message(spec)
    assert "agent/issue-42" in msg
    assert "test-unit" in msg
    assert "lint" in msg
    assert "{{" not in msg  # every placeholder substituted or stripped


def test_structured_extractor_plan_via_llm(monkeypatch):
    """structured_extractor extracts PlanOutput from LLM response with response_format."""
    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.chat.chat_completion import Choice

    from entrypoint import PlanOutput

    plan_json = json.dumps(
        {"issues": [{"id": 42, "title": "Fix auth", "branch": "agent/issue-42"}]}
    )
    mock_response = ChatCompletion(
        id="test",
        created=0,
        model="test",
        object="chat.completion",
        choices=[
            Choice(
                index=0,
                finish_reason="stop",
                message=ChatCompletionMessage(content=plan_json, role="assistant"),
            )
        ],
    )

    monkeypatch.setenv("AGENT_MODEL", "test-model")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "fake-key")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", "http://fake")

    from unittest.mock import MagicMock, patch

    with patch.object(entrypoint, "_get_llm_client") as mock_client_fn:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        mock_client_fn.return_value = mock_client

        result = entrypoint.structured_extractor("agent output text", PlanOutput)

    assert isinstance(result, PlanOutput)
    assert len(result.issues) == 1
    assert result.issues[0].id == 42
    assert result.issues[0].title == "Fix auth"


def test_structured_extractor_review_via_llm(monkeypatch):
    """structured_extractor extracts ReviewOutput including verdict and inline_comments."""
    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.chat.chat_completion import Choice

    from entrypoint import ReviewOutput

    review_json = json.dumps(
        {
            "summary": "looks good",
            "verdict": "lgtm",
            "inline_comments": [{"file": "a.py", "line": 3, "body": "nit"}],
        }
    )
    mock_response = ChatCompletion(
        id="test",
        created=0,
        model="test",
        object="chat.completion",
        choices=[
            Choice(
                index=0,
                finish_reason="stop",
                message=ChatCompletionMessage(content=review_json, role="assistant"),
            )
        ],
    )

    monkeypatch.setenv("AGENT_MODEL", "test-model")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "fake-key")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", "http://fake")

    from unittest.mock import MagicMock, patch

    with patch.object(entrypoint, "_get_llm_client") as mock_client_fn:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        mock_client_fn.return_value = mock_client

        result = entrypoint.structured_extractor("review text", ReviewOutput)

    assert isinstance(result, ReviewOutput)
    assert result.summary == "looks good"
    assert result.verdict == "lgtm"
    assert result.inline_comments[0].line == 3


def test_structured_extractor_strips_provider_prefix_from_model(monkeypatch):
    """AGENT_MODEL is configured litellm-style as "<provider>/<model>" (e.g.
    "openai/qwen3.6-27b-mtp") for the OpenHands LLM/litellm stack. The raw
    OpenAI SDK client used here talks directly to an OpenAI-compatible endpoint
    and rejects that prefixed name with "model not found" (caught in
    real-cluster testing of the github-webook-refactor branch — every
    structured extraction call failed in production config). Confirm the
    prefix is stripped before being sent to the client."""
    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.chat.chat_completion import Choice

    from entrypoint import PlanOutput

    plan_json = json.dumps(
        {"issues": [{"id": 1, "title": "x", "branch": "agent/issue-1"}]}
    )
    mock_response = ChatCompletion(
        id="test",
        created=0,
        model="test",
        object="chat.completion",
        choices=[
            Choice(
                index=0,
                finish_reason="stop",
                message=ChatCompletionMessage(content=plan_json, role="assistant"),
            )
        ],
    )

    monkeypatch.setenv("AGENT_MODEL", "openai/qwen3.6-27b-mtp")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "fake-key")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", "http://fake")

    from unittest.mock import MagicMock, patch

    with patch.object(entrypoint, "_get_llm_client") as mock_client_fn:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        mock_client_fn.return_value = mock_client

        entrypoint.structured_extractor("agent output text", PlanOutput)

    _, kwargs = mock_client.chat.completions.create.call_args
    assert kwargs["model"] == "qwen3.6-27b-mtp"


# --------------------------------------------------------------------------- #
# Review phase — comment-only, no commits (#55)
# --------------------------------------------------------------------------- #


def test_review_phase_produces_zero_commits(tmp_path, monkeypatch):
    """handle_review() must not execute any git commit commands — the Review
    phase is comment-only; branch history after Review contains zero new
    commits even if the agent edits files during analysis."""
    from unittest.mock import MagicMock, patch

    workdir = tmp_path / "repo"
    out_file = tmp_path / "out.json"
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
    _git("checkout", "-b", "agent/issue-42", cwd=seed)
    _git("remote", "add", "origin", str(bare), cwd=seed)
    _git("push", "origin", "main", cwd=seed)
    _git("push", "origin", "agent/issue-42", cwd=seed)

    def fake_run_agent(spec, wd, tracer):
        # Even if the agent modifies files, review must not commit them.
        Path(wd, "review-note.txt").write_text("review comment\n")
        return entrypoint.AgentOutcome(
            summary="Code looks clean. No issues found.",
        )

    monkeypatch.setattr(entrypoint, "run_agent", fake_run_agent)
    monkeypatch.setenv("AGENT_MODEL", "test-model")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "fake-key")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", "http://fake")

    review_json = json.dumps(
        {"summary": "Code looks clean.", "verdict": "lgtm", "inline_comments": []}
    )
    mock_response = MagicMock()
    mock_response.choices[0].message.content = review_json

    with patch.object(entrypoint, "_get_llm_client") as mock_client_fn:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        mock_client_fn.return_value = mock_client

        # Track subprocess calls — we care about git commit
        with patch("subprocess.run", wraps=subprocess.run) as run_mock:
            monkeypatch.setenv(
                "TASK_SPEC",
                json.dumps(
                    {
                        "phase": "review",
                        "project_id": "omneval",
                        "issue_number": 42,
                        "branch": "agent/issue-42",
                    }
                ),
            )
            monkeypatch.setenv("GITHUB_URL", str(bare))
            monkeypatch.setenv("DEFAULT_BRANCH", "main")
            monkeypatch.setenv("WORKDIR", str(workdir))
            monkeypatch.setenv("OUTPUT_CONFIGMAP", "agent-omneval-review-42-a1")
            monkeypatch.setenv("OUTPUT_FILE", str(out_file))
            monkeypatch.delenv("GITHUB_TOKEN", raising=False)

            rc = entrypoint.main()
            assert rc == 0

            # Verify no "git commit" was called
            commit_calls = [c for c in run_mock.call_args_list if "commit" in str(c)]
            assert not commit_calls, (
                f"handle_review() must not call git commit, but found: {commit_calls}"
            )

    payload = json.loads(out_file.read_text())
    assert payload["status"] == "complete"
    assert payload["commits"] == 0
    assert payload["review"]["verdict"] == "lgtm"


def test_review_prompt_has_no_commit_instructions(monkeypatch):
    """review.md must not instruct the agent to make commits or edit files —
    the Review phase is comment-only (#55)."""
    prompts_dir = Path(__file__).parent / "prompts"
    monkeypatch.setenv("AGENT_PROMPTS_DIR", str(prompts_dir))

    spec = entrypoint.TaskSpec(
        phase="review",
        project_id="omneval",
        issue_number=42,
        branch="agent/issue-42",
    )
    msg = entrypoint.build_agent_message(spec)

    commit_keywords = [
        "commit",
        "Make the changes directly",
        "do nothing",
        "preserve functionality",
    ]
    for kw in commit_keywords:
        assert kw.lower() not in msg.lower(), (
            f"review.md must not contain '{kw}' — Review phase is comment-only"
        )


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
    monkeypatch.setenv(
        "TASK_SPEC",
        json.dumps(
            {
                "phase": "execute",
                "project_id": "omneval",
                "issue_number": 7,
                "title": "x",
            }
        ),
    )
    monkeypatch.setenv("GITHUB_URL", str(origin))
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    monkeypatch.setenv("WORKDIR", str(workdir))
    monkeypatch.setenv("OUTPUT_CONFIGMAP", "j7")
    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    assert entrypoint.main() == 0
    assert json.loads(out_file.read_text())["status"] == "complete"


# --------------------------------------------------------------------------- #
# Per-phase skill allowlist parsing (issue #36)
# --------------------------------------------------------------------------- #


class TestLoadSkillsAllowlist:
    """Unit tests for _load_skills_allowlist(phase).

    The three-way semantics must hold:
      - Env var absent   → None            (all skills, backward-compat)
      - Env var = ""     → {phase: []}     (no skills)
      - Env var = "a,b"  → {phase: [a, b]} (exactly those skills)
    """

    def test_absent_env_returns_none(self, monkeypatch):
        """AGENT_SKILLS_ENABLED absent → None (all skills allowed)."""
        monkeypatch.delenv("AGENT_SKILLS_ENABLED", raising=False)
        result = entrypoint._load_skills_allowlist("execute")
        assert result is None

    def test_empty_string_returns_empty_phase_list(self, monkeypatch):
        """AGENT_SKILLS_ENABLED="" → {phase: []} (no skills allowed)."""
        monkeypatch.setenv("AGENT_SKILLS_ENABLED", "")
        result = entrypoint._load_skills_allowlist("execute")
        assert result == {"execute": []}

    def test_whitespace_only_returns_empty_phase_list(self, monkeypatch):
        """AGENT_SKILLS_ENABLED="   " (whitespace only) → {phase: []} (no skills)."""
        monkeypatch.setenv("AGENT_SKILLS_ENABLED", "   ")
        result = entrypoint._load_skills_allowlist("execute")
        assert result == {"execute": []}

    def test_single_name_returns_list(self, monkeypatch):
        """AGENT_SKILLS_ENABLED="tdd" → {phase: ["tdd"]}."""
        monkeypatch.setenv("AGENT_SKILLS_ENABLED", "tdd")
        result = entrypoint._load_skills_allowlist("execute")
        assert result == {"execute": ["tdd"]}

    def test_comma_separated_names_returns_list(self, monkeypatch):
        """AGENT_SKILLS_ENABLED="tdd,code-review" → {phase: ["tdd", "code-review"]}."""
        monkeypatch.setenv("AGENT_SKILLS_ENABLED", "tdd,code-review")
        result = entrypoint._load_skills_allowlist("execute")
        assert result == {"execute": ["tdd", "code-review"]}

    def test_names_with_spaces_are_stripped(self, monkeypatch):
        """Spaces around skill names in the comma-separated list are stripped."""
        monkeypatch.setenv("AGENT_SKILLS_ENABLED", " tdd , code-review ")
        result = entrypoint._load_skills_allowlist("execute")
        assert result == {"execute": ["tdd", "code-review"]}

    def test_phase_key_in_allowlist_matches_phase_arg(self, monkeypatch):
        """The phase key in the returned allowlist must match the ``phase`` argument."""
        monkeypatch.setenv("AGENT_SKILLS_ENABLED", "foo")
        result = entrypoint._load_skills_allowlist("review")
        assert result is not None
        assert "review" in result
        assert "execute" not in result

    def test_empty_segments_between_commas_are_ignored(self, monkeypatch):
        """'a,,b' → ["a", "b"] (empty segments dropped)."""
        monkeypatch.setenv("AGENT_SKILLS_ENABLED", "a,,b")
        result = entrypoint._load_skills_allowlist("execute")
        assert result == {"execute": ["a", "b"]}


def test_remediation_phase_pushes_fix(tmp_path, monkeypatch):
    """Remediation handler clones the branch, runs the agent, commits fixes,
    and pushes back when commits are produced."""
    workdir = tmp_path / "repo"
    out_file = tmp_path / "out.json"
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git("init", "--bare", "-b", "main", cwd=bare)

    # Seed repo with a branch that has a bug
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-b", "main", cwd=seed)
    _git("config", "user.email", "t@t.com", cwd=seed)
    _git("config", "user.name", "t", cwd=seed)
    (seed / "README.md").write_text("hello\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "init", cwd=seed)
    _git("checkout", "-b", "agent/issue-42", cwd=seed)
    (seed / "buggy.py").write_text("def broken(): pass\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "wip", cwd=seed)
    _git("remote", "add", "origin", str(bare), cwd=seed)
    _git("push", "origin", "main", cwd=seed)
    _git("push", "origin", "agent/issue-42", cwd=seed)

    def fake_run_agent(spec, wd, tracer):
        Path(wd, "buggy.py").write_text("def fixed(): return True\n")
        return entrypoint.AgentOutcome(summary="Fixed the bug", files_changed=True)

    monkeypatch.setattr(entrypoint, "run_agent", fake_run_agent)

    monkeypatch.setenv(
        "TASK_SPEC",
        json.dumps(
            {
                "phase": "remediation",
                "project_id": "omneval",
                "issue_number": 42,
                "title": "Fix CI",
                "branch": "agent/issue-42",
                "extra": {"ci_check_failures": "test-unit: exit code 1"},
            }
        ),
    )
    monkeypatch.setenv("GITHUB_URL", str(bare))
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    monkeypatch.setenv("WORKDIR", str(workdir))
    monkeypatch.setenv("OUTPUT_CONFIGMAP", "agent-omneval-remediation-42-a1")
    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    rc = entrypoint.main()
    assert rc == 0

    payload = json.loads(out_file.read_text())
    assert payload["status"] == "complete"
    assert payload["commits"] == 1
    assert payload["branch"] == "agent/issue-42"


def test_remediation_phase_no_fix_no_push(tmp_path, monkeypatch):
    """When the remediation agent produces no commits, the branch is not
    pushed and commits==0 in the result."""
    workdir = tmp_path / "repo"
    out_file = tmp_path / "out.json"
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git("init", "--bare", "-b", "main", cwd=bare)

    # Seed repo with a branch
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-b", "main", cwd=seed)
    _git("config", "user.email", "t@t.com", cwd=seed)
    _git("config", "user.name", "t", cwd=seed)
    (seed / "README.md").write_text("hello\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "init", cwd=seed)
    _git("checkout", "-b", "agent/issue-42", cwd=seed)
    _git("remote", "add", "origin", str(bare), cwd=seed)
    _git("push", "origin", "main", cwd=seed)
    _git("push", "origin", "agent/issue-42", cwd=seed)

    def fake_run_agent(spec, wd, tracer):
        # Agent makes no changes
        return entrypoint.AgentOutcome(summary="Could not fix", files_changed=False)

    monkeypatch.setattr(entrypoint, "run_agent", fake_run_agent)

    monkeypatch.setenv(
        "TASK_SPEC",
        json.dumps(
            {
                "phase": "remediation",
                "project_id": "omneval",
                "issue_number": 42,
                "title": "Fix CI",
                "branch": "agent/issue-42",
                "extra": {"ci_check_failures": "lint: exit code 1"},
            }
        ),
    )
    monkeypatch.setenv("GITHUB_URL", str(bare))
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    monkeypatch.setenv("WORKDIR", str(workdir))
    monkeypatch.setenv("OUTPUT_CONFIGMAP", "agent-omneval-remediation-42-a1")
    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    rc = entrypoint.main()
    assert rc == 0

    payload = json.loads(out_file.read_text())
    assert payload["status"] == "complete"
    assert payload["commits"] == 0


# --------------------------------------------------------------------------- #
# ConfigMap skills install at startup (issue #34)
# --------------------------------------------------------------------------- #


def test_main_installs_configmap_skills_when_configured(origin, tmp_path, monkeypatch):
    """When AGENT_SKILLS_CONFIGMAP is set, entrypoint installs staged skills."""
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "deploy-review").write_text("# ConfigMap Skill\n")
    convergence = tmp_path / "convergence"
    convergence.mkdir()

    workdir = tmp_path / "repo"
    out_file = tmp_path / "out.json"

    monkeypatch.setenv(
        "TASK_SPEC",
        json.dumps(
            {
                "phase": "execute",
                "project_id": "omneval",
                "issue_number": 5,
                "title": "Test",
                "body": "test",
                "instructions": "go",
            }
        ),
    )
    monkeypatch.setenv("GITHUB_URL", str(origin))
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    monkeypatch.setenv("WORKDIR", str(workdir))
    monkeypatch.setenv("OUTPUT_CONFIGMAP", "agent-omneval-execute-5-a1")
    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.setenv("AGENT_SKILLS_CONFIGMAP", "devloop-skills")
    monkeypatch.setenv("AGENT_SKILLS_STAGING_DIR", str(staging))
    monkeypatch.setenv("AGENT_SKILLS_DIR", str(convergence))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    # Mock run_agent so main() doesn't block on real agent work
    monkeypatch.setattr(
        entrypoint,
        "run_agent",
        lambda spec, wd, tracer: entrypoint.AgentOutcome(
            summary="test", files_changed=False
        ),
    )

    # Capture calls to install_configmap_skills
    install_calls = []
    real_fn = entrypoint.skills.install_configmap_skills

    def tracking_install(path):
        install_calls.append(path)
        return real_fn(path)

    monkeypatch.setattr(entrypoint.skills, "install_configmap_skills", tracking_install)

    rc = entrypoint.main()
    assert rc == 0
    assert len(install_calls) == 1
    assert install_calls[0] == str(staging)


def test_main_skips_configmap_install_when_not_configured(
    origin, tmp_path, monkeypatch
):
    """When AGENT_SKILLS_CONFIGMAP is unset, entrypoint does not install skills."""
    workdir = tmp_path / "repo"
    out_file = tmp_path / "out.json"

    monkeypatch.setenv(
        "TASK_SPEC",
        json.dumps(
            {
                "phase": "execute",
                "project_id": "omneval",
                "issue_number": 5,
                "title": "Test",
                "body": "test",
                "instructions": "go",
            }
        ),
    )
    monkeypatch.setenv("GITHUB_URL", str(origin))
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    monkeypatch.setenv("WORKDIR", str(workdir))
    monkeypatch.setenv("OUTPUT_CONFIGMAP", "agent-omneval-execute-5-a1")
    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.delenv("AGENT_SKILLS_CONFIGMAP", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    install_called = []

    def fake_install(path):
        install_called.append(path)
        return []

    monkeypatch.setattr(entrypoint.skills, "install_configmap_skills", fake_install)

    def fake_run_agent(spec, wd, tracer):
        Path(wd, "feature.txt").write_text("test\n")
        return entrypoint.AgentOutcome(summary="test", files_changed=True)

    monkeypatch.setattr(entrypoint, "run_agent", fake_run_agent)

    rc = entrypoint.main()
    assert rc == 0
    assert install_called == [], "install_configmap_skills should not be called"
