"""Tests for the agent runner seam (issue #121, ADR-0011).

The OpenHands path's behavior compatibility is proven by test_run_agent.py
passing unmodified (its fake openhands.sdk is still imported at the same
seam, now inside OpenHandsRunner). These tests cover runner *selection* and
the claude-agent-sdk runner with the SDK mocked via sys.modules injection.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

import runners
from entrypoint import TaskSpec


def _spec(phase: str = "execute") -> TaskSpec:
    return TaskSpec(phase=phase, project_id="proj", issue_number=1, title="t")


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
def test_resolve_runner_defaults_to_openhands(monkeypatch):
    monkeypatch.delenv("AGENT_RUNNER", raising=False)
    assert isinstance(runners.resolve_runner(), runners.OpenHandsRunner)


def test_resolve_runner_selects_claude_via_env(monkeypatch):
    monkeypatch.setenv("AGENT_RUNNER", "claude-agent-sdk")
    assert isinstance(runners.resolve_runner(), runners.ClaudeAgentSdkRunner)


def test_resolve_runner_explicit_name_wins_over_env(monkeypatch):
    monkeypatch.setenv("AGENT_RUNNER", "claude-agent-sdk")
    assert isinstance(runners.resolve_runner("openhands"), runners.OpenHandsRunner)


def test_resolve_runner_rejects_unknown_name(monkeypatch):
    monkeypatch.setenv("AGENT_RUNNER", "gpt-engineer")
    with pytest.raises(RuntimeError, match="unknown AGENT_RUNNER 'gpt-engineer'"):
        runners.resolve_runner()


# --------------------------------------------------------------------------- #
# Claude Agent SDK runner (SDK mocked)
# --------------------------------------------------------------------------- #
class _FakeResultMessage:
    """type(msg).__name__ must be 'ResultMessage' — match the real SDK."""

    def __init__(self, result: str, session_id: str):
        self.result = result
        self.session_id = session_id


# Renamed so type(msg).__name__ == "ResultMessage"
_FakeResultMessage.__name__ = "ResultMessage"


class _FakeClaudeSdk:
    """Installable stand-in for the claude_agent_sdk module."""

    def __init__(self, results: list[str]):
        self._results = list(results)
        self.queries: list[tuple[str, MagicMock]] = []
        self.options_cls = MagicMock(name="ClaudeAgentOptions")

    def install(self):
        mod = types.ModuleType("claude_agent_sdk")
        mod.ClaudeAgentOptions = self.options_cls

        async def _gen(prompt, options):
            self.queries.append((prompt, options))
            turn = len(self.queries)
            yield _FakeResultMessage(
                result=self._results.pop(0), session_id=f"sess-{turn}"
            )

        def query(*, prompt, options):
            return _gen(prompt, options)

        mod.query = query
        return mod


@pytest.fixture
def fake_claude_sdk(monkeypatch):
    sdk = _FakeClaudeSdk(results=["first answer", "resumed answer"])
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk.install())
    return sdk


def _llm_setting_stub(values: dict):
    def _setting(name, role="", default=None):
        if role:
            key = f"{name}_{role.upper()}"
            if key in values:
                return values[key]
        return values.get(name, default)

    return _setting


def test_claude_session_runs_a_turn_in_the_workspace(fake_claude_sdk):
    runner = runners.ClaudeAgentSdkRunner()
    session = runner.start(
        _spec(),
        "/workspace/repo",
        skills=None,
        build_agent=MagicMock(name="build_agent"),
        llm_setting=_llm_setting_stub(
            {"AGENT_MODEL": "claude-sonnet-4-6", "AGENT_LLM_API_KEY": "sk-ant-x"}
        ),
    )
    text = session.send("implement the issue")

    assert text == "first answer"
    prompt, _options = fake_claude_sdk.queries[0]
    assert prompt == "implement the issue"
    kwargs = fake_claude_sdk.options_cls.call_args.kwargs
    assert kwargs["cwd"] == "/workspace/repo"
    assert kwargs["permission_mode"] == "bypassPermissions"
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["env"] == {"ANTHROPIC_API_KEY": "sk-ant-x"}
    assert "resume" not in kwargs  # first turn — nothing to resume


def test_claude_runner_strips_litellm_provider_prefix_from_model(fake_claude_sdk):
    """AGENT_MODEL is configured litellm-style (e.g. "anthropic/claude-sonnet-4-6")
    for the openhands runner's routing; the Claude Agent SDK/CLI expects a bare
    model name or alias, so the "<provider>/" prefix must be stripped."""
    runner = runners.ClaudeAgentSdkRunner()
    session = runner.start(
        _spec(),
        "/w",
        skills=None,
        build_agent=MagicMock(),
        llm_setting=_llm_setting_stub({"AGENT_MODEL": "anthropic/claude-sonnet-4-6"}),
    )
    session.send("go")

    assert fake_claude_sdk.options_cls.call_args.kwargs["model"] == "claude-sonnet-4-6"


def test_claude_session_resumes_with_captured_session_id(fake_claude_sdk):
    runner = runners.ClaudeAgentSdkRunner()
    session = runner.start(
        _spec(),
        "/workspace/repo",
        skills=None,
        build_agent=MagicMock(),
        llm_setting=_llm_setting_stub({"AGENT_MODEL": "m"}),
    )
    assert session.send("turn one") == "first answer"
    assert session.send("Human answer: yes") == "resumed answer"

    second_kwargs = fake_claude_sdk.options_cls.call_args.kwargs
    assert second_kwargs["resume"] == "sess-1"


def test_claude_runner_uses_review_role_model(fake_claude_sdk):
    runner = runners.ClaudeAgentSdkRunner()
    runner.start(
        _spec(phase="review"),
        "/w",
        skills=None,
        build_agent=MagicMock(),
        llm_setting=_llm_setting_stub(
            {"AGENT_MODEL": "base", "AGENT_MODEL_REVIEW": "review-model"}
        ),
    ).send("review it")

    assert fake_claude_sdk.options_cls.call_args.kwargs["model"] == "review-model"


def test_claude_runner_ignores_skills_with_warning(fake_claude_sdk, caplog):
    runner = runners.ClaudeAgentSdkRunner()
    with caplog.at_level("WARNING"):
        runner.start(
            _spec(),
            "/w",
            skills=[object(), object()],
            build_agent=MagicMock(),
            llm_setting=_llm_setting_stub({}),
        )
    assert any("2 resolved skill(s) ignored" in r.message for r in caplog.records)
