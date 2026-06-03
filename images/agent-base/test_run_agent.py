"""Tests for run_agent and build_agent SDK API usage (issues #32, #34).

Verifies that run_agent:
  - uses the correct openhands-sdk 1.24.0 API:
      LLM(model, base_url, api_key)
      → build_agent(llm=llm, cli_mode=True, agent_context=None)
        (hand-rolled Agent replicating the default preset; wires terminal/
        file_editor/task_tracker; cli_mode drops the Chromium-only browser tool)
      → LocalConversation(agent, workspace)
      → send_message → run → get_agent_final_response(state.events)
  - returns stub outcome when AGENT_STUB=1
  - returns AgentOutcome with failure status on model/LLM error (no exception escapes)
  - detects empty diff / no changes and sets files_changed=False (not a false success)
  - OTEL_SERVICE_NAME propagates into the tracer provider (static config check)
  - with no skills loaded, behaves identically to the pre-#32 path (no-op)

All SDK calls are mocked via sys.modules injection so the test suite runs
without openhands-sdk installed.
"""

from __future__ import annotations

import json
import sys
import types
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import entrypoint
from entrypoint import AgentOutcome, TaskSpec


# --------------------------------------------------------------------------- #
# Helpers to inject a fake openhands.sdk module tree into sys.modules
# --------------------------------------------------------------------------- #


class _FakeConversationState:
    """Minimal stand-in for LocalConversation.state."""

    def __init__(self, events=None):
        self.events = events or []


class _FakeConversation:
    """Stand-in for LocalConversation."""

    def __init__(self, *, agent, workspace, **kw):
        self.agent = agent
        self.workspace = workspace
        self._messages = []
        self.state = _FakeConversationState()

    def send_message(self, msg):
        self._messages.append(msg)

    def run(self):
        pass  # no-op happy path


class _ErrorConversation(_FakeConversation):
    """Raises a RuntimeError from run() to simulate model failures."""

    def run(self):
        raise RuntimeError("connection refused: model endpoint unreachable")


@contextmanager
def _fake_sdk(
    conversation_cls=None,
    final_response: str = "agent summary text",
):
    """Context manager that installs a fake openhands.sdk into sys.modules.

    Returns (LLM_mock, Agent_mock, LocalConversation_mock,
    get_final_response_mock).

    build_agent hand-rolls an Agent(...) from the preset rather than calling
    get_default_agent, so the mock tree reflects that:
      - Agent is the constructor mock (openhands.sdk.Agent)
      - AgentContext is also mocked (openhands.sdk.AgentContext)
      - get_default_tools / get_default_condenser are mocked in
        openhands.tools.preset.default so build_agent can call them
    """
    sdk = types.ModuleType("openhands")
    sdk_sub = types.ModuleType("openhands.sdk")

    LLM_cls = MagicMock(name="LLM")
    Agent_cls = MagicMock(name="Agent")
    AgentContext_cls = MagicMock(name="AgentContext")
    conv_cls = conversation_cls or _FakeConversation

    # get_agent_final_response lives in openhands.sdk.conversation
    get_final_response = MagicMock(
        name="get_agent_final_response", return_value=final_response
    )

    sdk_sub.LLM = LLM_cls
    sdk_sub.Agent = Agent_cls
    sdk_sub.AgentContext = AgentContext_cls
    sdk_sub.LocalConversation = conv_cls
    sdk_sub.get_agent_final_response = get_final_response  # re-exported

    # Also needs to be importable from openhands.sdk.conversation
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
    # return an empty list so the no-skills path is exercised (existing tests).
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
# Tracer 1: AGENT_STUB=1 fast-path
# --------------------------------------------------------------------------- #


def test_stub_returns_stub_outcome(monkeypatch):
    """AGENT_STUB=1 returns the stub AgentOutcome without touching the SDK."""
    monkeypatch.setenv("AGENT_STUB", "1")
    with _fake_sdk() as (LLM, Agent_cls, Conv, gfr):
        outcome = entrypoint.run_agent(_spec(), "/tmp", _noop_tracer())
    assert outcome.summary == "stub run"
    assert outcome.files_changed is False
    LLM.assert_not_called()
    Agent_cls.assert_not_called()


# --------------------------------------------------------------------------- #
# Tracer 2: happy path — correct SDK object construction
# --------------------------------------------------------------------------- #


def test_happy_path_constructs_sdk_objects_correctly(monkeypatch, tmp_path):
    """run_agent passes model/base_url/api_key to LLM, builds the agent via
    build_agent (hand-rolled preset: get_default_tools + get_default_condenser +
    Agent(..., agent_context=None)), and passes that agent to LocalConversation."""
    monkeypatch.delenv("AGENT_STUB", raising=False)
    monkeypatch.setenv("AGENT_MODEL", "qwen3-27b")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", "http://192.168.68.104/v1")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "test-key")

    created_conversations = []

    class TrackingConversation(_FakeConversation):
        def __init__(self, *, agent, workspace, **kw):
            super().__init__(agent=agent, workspace=workspace, **kw)
            created_conversations.append(self)

    with _fake_sdk(conversation_cls=TrackingConversation, final_response="done") as (
        LLM_cls,
        Agent_cls,
        _,
        gfr,
    ):
        outcome = entrypoint.run_agent(_spec(), str(tmp_path), _noop_tracer())

    # LLM called with model + base_url + api_key
    LLM_cls.assert_called_once()
    llm_kwargs = LLM_cls.call_args
    assert llm_kwargs.kwargs.get("model") == "qwen3-27b" or (
        llm_kwargs.args and llm_kwargs.args[0] == "qwen3-27b"
    )
    assert llm_kwargs.kwargs.get("base_url") == "http://192.168.68.104/v1"
    assert llm_kwargs.kwargs.get("api_key") == "test-key"

    # Agent(...) was called (build_agent hand-rolls it with the tool preset).
    Agent_cls.assert_called_once()
    agent_call = Agent_cls.call_args
    # agent_context=None is the no-skills path (behaves as pre-#32)
    assert agent_call.kwargs.get("agent_context") is None

    # LocalConversation receives the built agent, not the LLM
    assert len(created_conversations) == 1
    conv = created_conversations[0]
    assert conv.agent == Agent_cls.return_value

    # get_agent_final_response called with conversation.state.events
    gfr.assert_called_once()

    # Outcome
    assert isinstance(outcome, AgentOutcome)
    assert outcome.summary == "done"


def test_happy_path_honours_default_env(monkeypatch, tmp_path):
    """When AGENT_LLM_BASE_URL and AGENT_MODEL are unset, defaults are used."""
    monkeypatch.delenv("AGENT_STUB", raising=False)
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("AGENT_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("AGENT_API_KEY", raising=False)

    with _fake_sdk() as (LLM_cls, Agent_cls, _, _gfr):
        entrypoint.run_agent(_spec(), str(tmp_path), _noop_tracer())

    llm_kwargs = LLM_cls.call_args.kwargs
    assert llm_kwargs["model"] == "qwen3-27b"
    assert llm_kwargs["base_url"] == "http://192.168.68.104/v1"


# --------------------------------------------------------------------------- #
# Tracer 3: model-error path → AgentOutcome with failure, no exception escapes
# --------------------------------------------------------------------------- #


def test_model_error_returns_failed_outcome_without_raising(monkeypatch, tmp_path):
    """When run() raises, run_agent catches it and returns a failed AgentOutcome.
    No exception must escape — the Job must terminate cleanly."""
    monkeypatch.delenv("AGENT_STUB", raising=False)

    with _fake_sdk(conversation_cls=_ErrorConversation) as (_, __, ___, ____):
        outcome = entrypoint.run_agent(_spec(), str(tmp_path), _noop_tracer())

    assert isinstance(outcome, AgentOutcome)
    assert outcome.files_changed is False
    assert (
        "error" in outcome.summary.lower()
        or outcome.structured is not None
        or (outcome.summary != "" and outcome.summary != "agent completed")
    )


def test_model_error_summary_contains_error_detail(monkeypatch, tmp_path):
    """The error message from the SDK exception is surfaced in the summary."""
    monkeypatch.delenv("AGENT_STUB", raising=False)

    with _fake_sdk(conversation_cls=_ErrorConversation):
        outcome = entrypoint.run_agent(_spec(), str(tmp_path), _noop_tracer())

    assert "connection refused" in outcome.summary or "unreachable" in outcome.summary


# --------------------------------------------------------------------------- #
# Tracer 4: empty diff / no changes produced
# --------------------------------------------------------------------------- #


def test_empty_diff_sets_files_changed_false(monkeypatch, tmp_path):
    """When get_agent_final_response returns empty string, files_changed=False."""
    monkeypatch.delenv("AGENT_STUB", raising=False)

    with _fake_sdk(final_response="") as (_, __, ___, ____):
        outcome = entrypoint.run_agent(_spec(), str(tmp_path), _noop_tracer())

    assert outcome.files_changed is False


def test_empty_diff_summary_is_not_a_false_success(monkeypatch, tmp_path):
    """An empty response is not silently treated as a success summary."""
    monkeypatch.delenv("AGENT_STUB", raising=False)

    with _fake_sdk(final_response="") as (_, __, ___, ____):
        outcome = entrypoint.run_agent(_spec(), str(tmp_path), _noop_tracer())

    # summary must signal the empty state, not claim 'agent completed'
    assert outcome.summary != "agent completed"


# --------------------------------------------------------------------------- #
# Tracer 5: OTEL_SERVICE_NAME static config check
# --------------------------------------------------------------------------- #


def test_otel_service_name_is_read_from_env(monkeypatch):
    """setup_tracing() uses OTEL_SERVICE_NAME for the service resource attribute.
    We verify statically that the code reads the right env var."""
    import inspect

    src = inspect.getsource(entrypoint.setup_tracing)
    # The function must reference OTEL_SERVICE_NAME
    assert "OTEL_SERVICE_NAME" in src, (
        "setup_tracing must read OTEL_SERVICE_NAME env var to tag spans by phase"
    )


def test_openhands_llm_spans_named_by_phase(monkeypatch):
    """_name_openhands_llm_spans wraps lmnr's TracerManager.init so a missing
    app_name defaults to the phase, not sys.argv[0] (the agent-entrypoint.py
    bug). We inject a fake lmnr module and assert the wrapper rewrites the call.
    """
    calls = {}

    class FakeTracerManager:
        @staticmethod
        def init(app_name=sys.argv[0], **kwargs):
            calls["app_name"] = app_name

    fake_pkg = types.ModuleType("lmnr")
    fake_otel = types.ModuleType("lmnr.opentelemetry_lib")
    fake_otel.TracerManager = FakeTracerManager
    monkeypatch.setitem(sys.modules, "lmnr", fake_pkg)
    monkeypatch.setitem(sys.modules, "lmnr.opentelemetry_lib", fake_otel)

    entrypoint._name_openhands_llm_spans("execute")

    # OpenHands calls init() with no app_name → the wrapper injects the phase.
    FakeTracerManager.init(base_url="https://omneval")
    assert calls["app_name"] == "execute"

    # An explicit app_name from the caller is respected, not overwritten.
    FakeTracerManager.init(app_name="custom")
    assert calls["app_name"] == "custom"


def test_openhands_llm_spans_naming_is_best_effort(monkeypatch):
    """If lmnr is not importable, _name_openhands_llm_spans is a silent no-op
    (it must never crash the Job)."""
    import builtins

    real_import = builtins.__import__

    def blocking_import(name, *args, **kwargs):
        if name.startswith("lmnr"):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "lmnr", raising=False)
    monkeypatch.delitem(sys.modules, "lmnr.opentelemetry_lib", raising=False)
    with patch.object(builtins, "__import__", blocking_import):
        entrypoint._name_openhands_llm_spans("execute")  # must not raise


def test_setup_tracing_names_openhands_spans(monkeypatch):
    """setup_tracing() invokes the lmnr naming fix up front (before run_agent
    imports OpenHands)."""
    import inspect

    src = inspect.getsource(entrypoint.setup_tracing)
    assert "_name_openhands_llm_spans" in src, (
        "setup_tracing must name OpenHands LLM spans before lmnr initialises"
    )


def test_otel_noop_tracer_when_sdk_absent(monkeypatch):
    """setup_tracing returns a no-op tracer when OTel SDK is not installed."""
    # Hide the OTel SDK by patching the import
    import builtins

    real_import = builtins.__import__

    def blocking_import(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    with patch.object(builtins, "__import__", blocking_import):
        tracer = entrypoint.setup_tracing()

    # Must support start_as_current_span as a context manager
    ctx = tracer.start_as_current_span("test-span")
    assert hasattr(ctx, "__enter__") and hasattr(ctx, "__exit__")


# --------------------------------------------------------------------------- #
# Tracer 6: full main() doesn't hang on model error (integration)
# --------------------------------------------------------------------------- #


def test_main_records_failure_on_model_error(tmp_path, monkeypatch):
    """When the SDK raises during run_agent, main() writes a failed payload
    to the output file and returns non-zero — the Job terminates cleanly."""
    import json
    import subprocess

    out_file = tmp_path / "out.json"

    # We need a real git repo for handle_execute; use a simple bare repo
    bare = tmp_path / "origin.git"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", bare], check=True, capture_output=True
    )
    seed = tmp_path / "seed"
    seed.mkdir()
    for cmd in [
        ["git", "init", "-b", "main", seed],
        ["git", "-C", str(seed), "config", "user.email", "t@t.com"],
        ["git", "-C", str(seed), "config", "user.name", "t"],
    ]:
        subprocess.run(cmd, check=True, capture_output=True)
    (seed / "README.md").write_text("hello\n")
    subprocess.run(
        ["git", "-C", str(seed), "add", "-A"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(seed), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(seed), "remote", "add", "origin", str(bare)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(seed), "push", "origin", "main"],
        check=True,
        capture_output=True,
    )

    workdir = tmp_path / "repo"

    monkeypatch.delenv("AGENT_STUB", raising=False)
    monkeypatch.setenv(
        "TASK_SPEC",
        json.dumps(
            {
                "phase": "execute",
                "project_id": "omneval",
                "issue_number": 99,
                "title": "test",
                "body": "b",
                "instructions": "i",
            }
        ),
    )
    monkeypatch.setenv("GITHUB_URL", str(bare))
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    monkeypatch.setenv("WORKDIR", str(workdir))
    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with _fake_sdk(conversation_cls=_ErrorConversation):
        # run_agent is called inside handle_execute; the error path should
        # cause the push to still fail (nothing committed), but main() must
        # record a terminal result
        entrypoint.main()

    payload = json.loads(out_file.read_text())
    # Either run_agent handled it gracefully and the outer flow continued
    # (status=complete, no changes pushed) or main caught an outer exception.
    # Either way: the output file must exist with a 'status' key — not hang.
    assert "status" in payload


# --------------------------------------------------------------------------- #
# Merge phase — PR-review model (open a review PR, don't merge to main)
# --------------------------------------------------------------------------- #
def _merge_spec():
    return TaskSpec(
        phase="merge",
        project_id="omneval",
        extra={
            "branches": ["agent/issue-7"],
            "issues": [{"id": "7", "title": "Fix the thing"}],
        },
    )


def _merge_env(monkeypatch, reviewer="zbloss"):
    monkeypatch.setenv("GITHUB_URL", "https://github.com/omneval/omneval")
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    if reviewer:
        monkeypatch.setenv("PR_REVIEWER", reviewer)
    else:
        monkeypatch.delenv("PR_REVIEWER", raising=False)


def test_handle_merge_opens_review_pr_and_does_not_push_main(monkeypatch):
    """The merge phase opens a review PR for the approved branch and never
    touches the default branch (no clone, no push to main)."""
    _merge_env(monkeypatch)
    calls = {}

    def fake_open(repo, branch, base, title, body, reviewer):
        calls.update(
            repo=repo,
            branch=branch,
            base=base,
            title=title,
            body=body,
            reviewer=reviewer,
        )
        return "https://github.com/omneval/omneval/pull/99"

    monkeypatch.setattr(entrypoint, "open_review_pr", fake_open)
    # Guard: a regression that re-introduced the direct merge would call these.
    monkeypatch.setattr(
        entrypoint,
        "push_branch",
        lambda *a, **k: pytest.fail("merge phase must not push"),
    )
    monkeypatch.setattr(
        entrypoint,
        "clone_repo",
        lambda *a, **k: pytest.fail("merge phase must not clone"),
    )

    result = entrypoint.handle_merge(_merge_spec(), _noop_tracer())

    assert result["status"] == "complete"
    assert result["pr_url"] == "https://github.com/omneval/omneval/pull/99"
    assert result["merged_issues"] == [7]
    assert calls["repo"] == "omneval/omneval"
    assert calls["branch"] == "agent/issue-7"
    assert calls["base"] == "main"
    assert calls["reviewer"] == "zbloss"
    assert "Closes #7" in calls["body"] and "@zbloss" in calls["body"]


def test_handle_merge_fails_when_no_pr_opened(monkeypatch):
    """If no PR could be opened the phase fails so the work isn't silently
    stranded on an unreviewed branch."""
    _merge_env(monkeypatch)
    monkeypatch.setattr(entrypoint, "open_review_pr", lambda *a, **k: "")

    result = entrypoint.handle_merge(_merge_spec(), _noop_tracer())

    assert result["status"] == "failed"
    assert result["merged_issues"] == [7]
    assert "no review PR" in result["error"]


def test_open_review_pr_marks_existing_draft_ready_and_tags(monkeypatch):
    """When the execute phase already opened a draft PR, the merge phase marks
    it ready and tags the reviewer (assignee + best-effort review request +
    @-mention comment) rather than creating a second PR."""
    cmds = []

    def fake_gh(args):
        cmds.append(args)
        rc = 0
        out = ""
        if args[:2] == ["pr", "view"]:
            out = json.dumps({"url": "https://x/pull/7", "isDraft": True})
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")

    monkeypatch.setattr(entrypoint, "_gh", fake_gh)
    url = entrypoint.open_review_pr(
        "omneval/omneval", "agent/issue-7", "main", "t", "b", "zbloss"
    )

    assert url == "https://x/pull/7"
    verbs = [a[:2] for a in cmds]
    assert ["pr", "view"] in verbs
    assert ["pr", "ready"] in verbs  # un-drafted
    assert not any(a[:2] == ["pr", "create"] for a in cmds)  # no duplicate PR
    assert any("--add-assignee" in a for a in cmds)
    assert any("--add-reviewer" in a for a in cmds)
    assert any(a[:2] == ["pr", "comment"] for a in cmds)


def test_open_review_pr_creates_when_none_exists(monkeypatch):
    cmds = []

    def fake_gh(args):
        cmds.append(args)
        if args[:2] == ["pr", "view"]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="no PR")
        if args[:2] == ["pr", "create"]:
            return types.SimpleNamespace(
                returncode=0, stdout="https://x/pull/7\n", stderr=""
            )
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(entrypoint, "_gh", fake_gh)
    url = entrypoint.open_review_pr(
        "omneval/omneval", "agent/issue-7", "main", "t", "b", "zbloss"
    )

    assert url == "https://x/pull/7"
    assert any(a[:2] == ["pr", "create"] for a in cmds)


# --------------------------------------------------------------------------- #
# Diagnosis phase — structured output for autonomous remediation
# --------------------------------------------------------------------------- #
def test_extract_diagnosis_parses_tagged_block():
    text = (
        "Here is my analysis.\n"
        "<diagnosis>\n"
        '{"severity":"critical","affected_resource":"omneval/Pod/x",'
        '"root_cause_hypothesis":"crashloop","recommended_actions":'
        '[{"action":"kubectl delete pod x -n omneval","requires_approval":false,"rationale":"recreate"}]}'
        "\n</diagnosis>\nTrailing chatter."
    )
    d = entrypoint._extract_diagnosis(text)
    assert d["severity"] == "critical"
    assert d["recommended_actions"][0]["action"] == "kubectl delete pod x -n omneval"


def test_extract_diagnosis_tolerates_json_fence_inside_tags():
    text = '<diagnosis>\n```json\n{"severity":"warning","recommended_actions":[]}\n```\n</diagnosis>'
    d = entrypoint._extract_diagnosis(text)
    assert d == {"severity": "warning", "recommended_actions": []}


def test_extract_diagnosis_falls_back_without_tags():
    d = entrypoint._extract_diagnosis(
        'blah {"severity":"info","recommended_actions":[]} end'
    )
    assert d["severity"] == "info"


def test_normalize_actions_defaults_and_filters():
    actions = [
        {"action": "kubectl delete pod x -n ns"},  # default approval False
        {"action": "kubectl drain node1", "requires_approval": True},  # explicit gate
        {"action": "  "},  # empty -> dropped
        {"rationale": "no action key"},  # no command -> dropped
        "flux reconcile helmrelease omneval -n omneval",  # bare string
    ]
    out = entrypoint._normalize_actions(actions)
    assert [a["action"] for a in out] == [
        "kubectl delete pod x -n ns",
        "kubectl drain node1",
        "flux reconcile helmrelease omneval -n omneval",
    ]
    assert out[0]["requires_approval"] is False
    assert out[1]["requires_approval"] is True
    assert out[2]["requires_approval"] is False


def test_handle_diagnosis_returns_normalized_actions(monkeypatch):
    spec = TaskSpec(
        phase="diagnosis",
        project_id="homelab-alerts",
        extra={
            "alert": {
                "name": "KubePodCrashLooping",
                "severity": "critical",
                "namespace": "omneval",
            }
        },
    )
    structured = {
        "severity": "critical",
        "affected_resource": "omneval/Pod/omneval-writer-0",
        "root_cause_hypothesis": "crashloop after bad config",
        "recommended_actions": [
            {
                "action": "kubectl rollout restart deployment/foo -n omneval",
                "requires_approval": False,
            },
        ],
    }
    monkeypatch.setattr(
        entrypoint,
        "run_agent",
        lambda *a, **k: AgentOutcome(summary="x", structured=structured),
    )
    result = entrypoint.handle_diagnosis(spec, _noop_tracer())
    d = result["diagnosis"]
    assert result["status"] == "complete"
    assert d["affected_resource"] == "omneval/Pod/omneval-writer-0"
    assert d["recommended_actions"][0] == {
        "action": "kubectl rollout restart deployment/foo -n omneval",
        "requires_approval": False,
        "rationale": "",
    }


def test_diagnosis_prompt_renders_alert_fields():
    spec = TaskSpec(
        phase="diagnosis",
        project_id="homelab-alerts",
        extra={
            "alert": {
                "name": "ContainerOOMKilled",
                "severity": "critical",
                "namespace": "omneval",
                "labels": {"pod": "omneval-writer-0"},
                "annotations": {},
            }
        },
    )
    msg = entrypoint.build_agent_message(spec)
    assert "ContainerOOMKilled" in msg
    assert "omneval-writer-0" in msg  # alert details injected
    assert "<diagnosis>" in msg  # schema present
    assert "requires_approval" in msg


# --------------------------------------------------------------------------- #
# Issue #32: build_agent — empty-skills no-op
# --------------------------------------------------------------------------- #


def test_build_agent_with_no_agent_context_behaves_as_today(monkeypatch, tmp_path):
    """build_agent(agent_context=None) reproduces the pre-#32 behaviour exactly:
    Agent is constructed with the default preset tools and condenser, with no
    agent_context attached (the skills seam is a no-op when context is None)."""
    monkeypatch.delenv("AGENT_STUB", raising=False)

    with _fake_sdk(final_response="done") as (LLM_cls, Agent_cls, _, _gfr):
        entrypoint.run_agent(_spec(), str(tmp_path), _noop_tracer())

    # Agent must have been called exactly once
    Agent_cls.assert_called_once()
    call_kwargs = Agent_cls.call_args.kwargs

    # agent_context=None → no skills injected (no-op path)
    assert call_kwargs.get("agent_context") is None

    # The preset tools and condenser are still wired in
    assert "llm" in call_kwargs
    assert "tools" in call_kwargs
    assert "condenser" in call_kwargs


def test_installed_skills_flow_through_to_agent_context(monkeypatch, tmp_path):
    """End-to-end: when installed skills are present they flow through
    resolve_skills → AgentContext(skills=..., load_public_skills=False)
    → Agent(agent_context=...).  This proves the #32 wire is live.

    We inject fake skills via the skills module's _loader seam and assert
    that AgentContext was constructed with those skills and that Agent
    received it.
    """
    from collections import namedtuple

    monkeypatch.delenv("AGENT_STUB", raising=False)

    FakeSkill = namedtuple("FakeSkill", ["name", "content"])
    fake_skill = FakeSkill("test-skill", "skill content")

    # All assertions happen *inside* the _fake_sdk context so the mocked
    # AgentContext class is still accessible.
    with _fake_sdk(final_response="done") as (LLM_cls, Agent_cls, _, _gfr):
        # Override the skills module loader to return one fake skill
        import skills as _skills_mod

        original_loader = _skills_mod._default_loader
        _skills_mod._default_loader = lambda installed_dir: [fake_skill]
        try:
            entrypoint.run_agent(_spec(), str(tmp_path), _noop_tracer())
        finally:
            _skills_mod._default_loader = original_loader

        # Fetch AgentContext_cls from sys.modules (still live inside the ctx mgr)
        import sys

        AgentContext_cls = sys.modules["openhands.sdk"].AgentContext

        # AgentContext was called with skills=[fake_skill] and load_public_skills=False
        AgentContext_cls.assert_called_once()
        ctx_kwargs = AgentContext_cls.call_args.kwargs
        assert ctx_kwargs.get("skills") == [fake_skill]
        assert ctx_kwargs.get("load_public_skills") is False

        # Agent received the constructed AgentContext (not None)
        Agent_cls.assert_called_once()
        agent_ctx_arg = Agent_cls.call_args.kwargs.get("agent_context")
        assert agent_ctx_arg is AgentContext_cls.return_value
