"""Tests for the mid-run human-question flow (issue #36).

Verifies that:
1. request_human_input writes ``awaiting_human`` + question to the output sink,
   polls for an answer, and returns (answer, False) when an answer arrives.
2. request_human_input returns ("", True) when HUMAN_ANSWER_TIMEOUT_SECONDS
   elapses without an answer (best-guess path).
3. run_agent detects a QUESTION: prefixed line in the final response, calls
   request_human_input, feeds the answer back, and returns the resumed outcome.
4. run_agent detects a QUESTION: line, times out, proceeds with a best-guess
   instruction, and records the assumption in the outcome summary.
5. A normal no-question run still works unchanged (regression).

Env seams (no cluster required):
- OUTPUT_FILE  → write_output writes here; we read it to assert awaiting_human
- HUMAN_ANSWER_FILE → read_human_answer reads from here
- HUMAN_ANSWER_TIMEOUT_SECONDS=0 → instant timeout
- HUMAN_ANSWER_POLL_SECONDS=0    → no real sleep
"""

from __future__ import annotations

import json
import sys
import types
from contextlib import contextmanager
from unittest.mock import MagicMock


import entrypoint
from entrypoint import AgentOutcome, TaskSpec


# --------------------------------------------------------------------------- #
# Shared helpers (mirrors test_run_agent.py helpers)
# --------------------------------------------------------------------------- #


class _FakeConversationState:
    def __init__(self, events=None):
        self.events = events or []


class _FakeConversation:
    """Stand-in for LocalConversation.  Configurable via class-level attributes."""

    # Subclasses override this list to control what get_agent_final_response
    # returns on each successive call.
    responses: list[str] = ["agent summary text"]

    def __init__(self, *, agent, workspace, **kw):
        self.agent = agent
        self.workspace = workspace
        self._messages: list[str] = []
        self.state = _FakeConversationState()
        self._run_count = 0

    def send_message(self, msg: str) -> None:
        self._messages.append(msg)

    def run(self) -> None:
        self._run_count += 1


@contextmanager
def _fake_sdk(conversation_cls=None, responses: list[str] | None = None):
    """Install a fake openhands.sdk; get_agent_final_response cycles through
    ``responses`` on successive calls (defaults to ["agent summary text"]).
    """
    if responses is None:
        responses = ["agent summary text"]

    call_count = [0]

    def _get_final_response(events):
        val = (
            responses[call_count[0]]
            if call_count[0] < len(responses)
            else responses[-1]
        )
        call_count[0] += 1
        return val

    sdk = types.ModuleType("openhands")
    sdk_sub = types.ModuleType("openhands.sdk")

    LLM_cls = MagicMock(name="LLM")
    Agent_cls = MagicMock(name="Agent")
    AgentContext_cls = MagicMock(name="AgentContext")
    conv_cls = conversation_cls or _FakeConversation

    get_final_response = MagicMock(
        name="get_agent_final_response", side_effect=_get_final_response
    )

    sdk_sub.LLM = LLM_cls
    sdk_sub.Agent = Agent_cls
    sdk_sub.AgentContext = AgentContext_cls
    sdk_sub.LocalConversation = conv_cls
    sdk_sub.get_agent_final_response = get_final_response

    conv_mod = types.ModuleType("openhands.sdk.conversation")
    conv_mod.get_agent_final_response = get_final_response

    # build_agent imports get_default_tools / get_default_condenser from preset
    get_default_tools = MagicMock(name="get_default_tools", return_value=[])
    get_default_condenser = MagicMock(name="get_default_condenser")
    tools_mod = types.ModuleType("openhands.tools")
    preset_mod = types.ModuleType("openhands.tools.preset")
    preset_default_mod = types.ModuleType("openhands.tools.preset.default")
    preset_default_mod.get_default_tools = get_default_tools
    preset_default_mod.get_default_condenser = get_default_condenser

    # skills.py's default loader imports load_installed_skills from this path;
    # return an empty list so the no-skills path is exercised.
    skills_installed_mod = types.ModuleType("openhands.sdk.skills.installed")
    skills_installed_mod.load_installed_skills = MagicMock(
        name="load_installed_skills", return_value=[]
    )
    sdk_skills_mod = types.ModuleType("openhands.sdk.skills")

    old = {}
    keys = (
        "openhands",
        "openhands.sdk",
        "openhands.sdk.conversation",
        "openhands.sdk.skills",
        "openhands.sdk.skills.installed",
        "openhands.tools",
        "openhands.tools.preset",
        "openhands.tools.preset.default",
    )
    for key in keys:
        old[key] = sys.modules.get(key)

    sys.modules["openhands"] = sdk
    sys.modules["openhands.sdk"] = sdk_sub
    sys.modules["openhands.sdk.conversation"] = conv_mod
    sys.modules["openhands.sdk.skills"] = sdk_skills_mod
    sys.modules["openhands.sdk.skills.installed"] = skills_installed_mod
    sys.modules["openhands.tools"] = tools_mod
    sys.modules["openhands.tools.preset"] = preset_mod
    sys.modules["openhands.tools.preset.default"] = preset_default_mod

    try:
        yield LLM_cls, Agent_cls, conv_cls, get_final_response
    finally:
        for key, val in old.items():
            if val is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = val


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
        issue_number=1,
        title="T",
        body="B",
        instructions="I",
        branch="",
    )
    defaults.update(kw)
    return TaskSpec(**defaults)


# --------------------------------------------------------------------------- #
# Slice 1: request_human_input — answer arrives
# --------------------------------------------------------------------------- #


def test_request_human_input_writes_awaiting_human_and_question(tmp_path, monkeypatch):
    """request_human_input writes {status: awaiting_human, question: ...} to OUTPUT_FILE."""
    out_file = tmp_path / "out.json"
    answer_file = tmp_path / "answer.txt"
    # Pre-seed the answer so the function returns immediately
    answer_file.write_text("the answer")

    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.setenv("HUMAN_ANSWER_FILE", str(answer_file))
    monkeypatch.setenv("HUMAN_ANSWER_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("HUMAN_ANSWER_POLL_SECONDS", "0")

    answer, timed_out = entrypoint.request_human_input("What colour?")

    payload = json.loads(out_file.read_text())
    assert payload["status"] == "awaiting_human"
    assert payload["question"] == "What colour?"
    assert answer == "the answer"
    assert timed_out is False


def test_request_human_input_returns_answer_and_not_timed_out(tmp_path, monkeypatch):
    """Returns (answer, False) when the answer is available."""
    out_file = tmp_path / "out.json"
    answer_file = tmp_path / "answer.txt"
    answer_file.write_text("blue")

    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.setenv("HUMAN_ANSWER_FILE", str(answer_file))
    monkeypatch.setenv("HUMAN_ANSWER_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("HUMAN_ANSWER_POLL_SECONDS", "0")

    answer, timed_out = entrypoint.request_human_input("Favourite colour?")

    assert answer == "blue"
    assert timed_out is False


# --------------------------------------------------------------------------- #
# Slice 2: request_human_input — timeout path
# --------------------------------------------------------------------------- #


def test_request_human_input_times_out_when_no_answer(tmp_path, monkeypatch):
    """With timeout=0 and no answer file, returns ("", True) immediately."""
    out_file = tmp_path / "out.json"
    answer_file = tmp_path / "answer.txt"
    # Do NOT write the answer file — simulates no reply

    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.setenv("HUMAN_ANSWER_FILE", str(answer_file))
    monkeypatch.setenv("HUMAN_ANSWER_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("HUMAN_ANSWER_POLL_SECONDS", "0")

    answer, timed_out = entrypoint.request_human_input("Shall I continue?")

    assert answer == ""
    assert timed_out is True


def test_request_human_input_still_writes_awaiting_human_on_timeout(
    tmp_path, monkeypatch
):
    """Even when it times out, awaiting_human was written before polling started."""
    out_file = tmp_path / "out.json"
    answer_file = tmp_path / "answer.txt"

    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.setenv("HUMAN_ANSWER_FILE", str(answer_file))
    monkeypatch.setenv("HUMAN_ANSWER_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("HUMAN_ANSWER_POLL_SECONDS", "0")

    entrypoint.request_human_input("Are you sure?")

    payload = json.loads(out_file.read_text())
    assert payload["status"] == "awaiting_human"
    assert payload["question"] == "Are you sure?"


# --------------------------------------------------------------------------- #
# Slice 3: _extract_question helper
# --------------------------------------------------------------------------- #


def test_extract_question_detects_question_prefix():
    """_extract_question returns the question text when response contains QUESTION:."""
    text = "I looked at the code.\nQUESTION: Should I use async or sync?\nMore text."
    q = entrypoint._extract_question(text)
    assert q == "Should I use async or sync?"


def test_extract_question_returns_none_when_no_question():
    """_extract_question returns None for normal (non-question) responses."""
    text = "I implemented the feature as requested."
    q = entrypoint._extract_question(text)
    assert q is None


def test_extract_question_returns_none_for_empty_text():
    """_extract_question returns None for empty string."""
    assert entrypoint._extract_question("") is None


# --------------------------------------------------------------------------- #
# Slice 4: run_agent — ask → answer → resume
# --------------------------------------------------------------------------- #


def test_run_agent_detects_question_and_resumes_with_answer(tmp_path, monkeypatch):
    """When the first response contains QUESTION:, run_agent writes awaiting_human,
    waits for the answer, feeds it back, and returns the resumed outcome."""
    out_file = tmp_path / "out.json"
    answer_file = tmp_path / "answer.txt"
    # Pre-seed the human answer
    answer_file.write_text("Use async")

    monkeypatch.delenv("AGENT_STUB", raising=False)
    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.setenv("HUMAN_ANSWER_FILE", str(answer_file))
    monkeypatch.setenv("HUMAN_ANSWER_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("HUMAN_ANSWER_POLL_SECONDS", "0")

    # First response has QUESTION:, second is the resumed final answer
    with _fake_sdk(
        responses=["QUESTION: Should I use async or sync?", "Implemented using async."]
    ):
        outcome = entrypoint.run_agent(_spec(), str(tmp_path), _noop_tracer())

    # The output file should have had awaiting_human written at some point
    # (the final write_output in run_agent is terminal, but the intermediate was awaiting_human)
    # We check the outcome: it should contain the resumed summary
    assert isinstance(outcome, AgentOutcome)
    assert (
        "async" in outcome.summary.lower() or "implemented" in outcome.summary.lower()
    )
    assert outcome.files_changed is True  # resumed successfully


def test_run_agent_question_causes_awaiting_human_written_to_output(
    tmp_path, monkeypatch
):
    """After detecting QUESTION:, run_agent writes awaiting_human to the output sink."""
    out_file = tmp_path / "out.json"
    answer_file = tmp_path / "answer.txt"
    answer_file.write_text("Go with sync")

    monkeypatch.delenv("AGENT_STUB", raising=False)
    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.setenv("HUMAN_ANSWER_FILE", str(answer_file))
    monkeypatch.setenv("HUMAN_ANSWER_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("HUMAN_ANSWER_POLL_SECONDS", "0")

    # Capture what was written to output over time via a spy
    writes: list[dict] = []
    original_write = entrypoint.write_output

    def _spy_write(payload):
        writes.append(dict(payload))
        original_write(payload)

    monkeypatch.setattr(entrypoint, "write_output", _spy_write)

    with _fake_sdk(
        responses=["QUESTION: Which approach?", "Used the correct approach."]
    ):
        entrypoint.run_agent(_spec(), str(tmp_path), _noop_tracer())

    # At least one write must have been awaiting_human
    statuses = [w.get("status") for w in writes]
    assert "awaiting_human" in statuses, (
        f"Expected awaiting_human in writes; got: {writes}"
    )


def test_run_agent_answer_is_fed_back_into_conversation(tmp_path, monkeypatch):
    """The human answer is sent back into the conversation (send_message called twice)."""
    out_file = tmp_path / "out.json"
    answer_file = tmp_path / "answer.txt"
    answer_file.write_text("Use the typed approach")

    monkeypatch.delenv("AGENT_STUB", raising=False)
    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.setenv("HUMAN_ANSWER_FILE", str(answer_file))
    monkeypatch.setenv("HUMAN_ANSWER_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("HUMAN_ANSWER_POLL_SECONDS", "0")

    created_conversations = []

    class TrackingConversation(_FakeConversation):
        def __init__(self, *, agent, workspace, **kw):
            super().__init__(agent=agent, workspace=workspace, **kw)
            created_conversations.append(self)

    with _fake_sdk(
        conversation_cls=TrackingConversation,
        responses=["QUESTION: Typed or untyped?", "Implemented with typed approach."],
    ):
        entrypoint.run_agent(_spec(), str(tmp_path), _noop_tracer())

    # The conversation should have received the answer as a second message
    assert len(created_conversations) >= 1
    conv = created_conversations[0]
    assert any(
        "typed" in m.lower() or "use the typed" in m.lower() for m in conv._messages
    ), f"Expected answer in conversation messages; got: {conv._messages}"


# --------------------------------------------------------------------------- #
# Slice 5: run_agent — ask → timeout → best-guess
# --------------------------------------------------------------------------- #


def test_run_agent_timeout_proceeds_with_best_guess(tmp_path, monkeypatch):
    """When request_human_input times out, run_agent proceeds with best-guess
    and returns an outcome (does not hang or raise)."""
    out_file = tmp_path / "out.json"
    answer_file = tmp_path / "answer.txt"
    # No answer — will time out immediately

    monkeypatch.delenv("AGENT_STUB", raising=False)
    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.setenv("HUMAN_ANSWER_FILE", str(answer_file))
    monkeypatch.setenv("HUMAN_ANSWER_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("HUMAN_ANSWER_POLL_SECONDS", "0")

    with _fake_sdk(
        responses=["QUESTION: Should I refactor?", "Proceeded with best assumption."]
    ):
        outcome = entrypoint.run_agent(_spec(), str(tmp_path), _noop_tracer())

    assert isinstance(outcome, AgentOutcome)
    # Must not hang; must return something
    assert outcome is not None


def test_run_agent_timeout_records_assumption_in_summary(tmp_path, monkeypatch):
    """On timeout, the outcome summary documents the best-guess assumption."""
    out_file = tmp_path / "out.json"
    answer_file = tmp_path / "answer.txt"

    monkeypatch.delenv("AGENT_STUB", raising=False)
    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.setenv("HUMAN_ANSWER_FILE", str(answer_file))
    monkeypatch.setenv("HUMAN_ANSWER_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("HUMAN_ANSWER_POLL_SECONDS", "0")

    with _fake_sdk(responses=["QUESTION: Use new API?", "Used new API by assumption."]):
        outcome = entrypoint.run_agent(_spec(), str(tmp_path), _noop_tracer())

    # The summary must mention that a best-guess assumption was made
    assert (
        "assumption" in outcome.summary.lower()
        or "best guess" in outcome.summary.lower()
        or (
            "no answer" in outcome.summary.lower()
            or "timed out" in outcome.summary.lower()
        )
    ), f"Expected assumption note in summary; got: {outcome.summary!r}"


# --------------------------------------------------------------------------- #
# Slice 6: regression — normal no-question run unchanged
# --------------------------------------------------------------------------- #


def test_normal_run_without_question_unchanged(tmp_path, monkeypatch):
    """A normal run (no QUESTION: in response) works exactly as before."""
    out_file = tmp_path / "out.json"

    monkeypatch.delenv("AGENT_STUB", raising=False)
    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.delenv("HUMAN_ANSWER_FILE", raising=False)
    monkeypatch.setenv("HUMAN_ANSWER_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("HUMAN_ANSWER_POLL_SECONDS", "0")

    with _fake_sdk(responses=["Implemented the feature as requested."]):
        outcome = entrypoint.run_agent(_spec(), str(tmp_path), _noop_tracer())

    assert isinstance(outcome, AgentOutcome)
    assert outcome.summary == "Implemented the feature as requested."
    assert outcome.files_changed is True


def test_stub_mode_not_affected_by_human_question_changes(monkeypatch, tmp_path):
    """AGENT_STUB=1 still returns the stub outcome immediately."""
    monkeypatch.setenv("AGENT_STUB", "1")
    monkeypatch.setenv("OUTPUT_FILE", str(tmp_path / "out.json"))
    monkeypatch.setenv("HUMAN_ANSWER_TIMEOUT_SECONDS", "0")

    with _fake_sdk(responses=["QUESTION: Are you there?"]):
        outcome = entrypoint.run_agent(_spec(), str(tmp_path), _noop_tracer())

    assert outcome.summary == "stub run"
    assert outcome.files_changed is False
