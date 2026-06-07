"""Integration test: AGENT_STUB=1 stub round-trip (issue #37).

Exercises both sides of the dispatch → poll → output-ConfigMap contract
together, without a cluster:

1. render_job propagates AGENT_STUB into the Job container env when set.
2. The entrypoint stub path (AGENT_STUB=1) writes a valid output payload via
   OUTPUT_FILE that the worker can parse.
3. _read_output / AgentJobResult.from_payload returns a terminal AgentJobResult
   — i.e. the workflow would advance.
4. A fake-k8s dispatch_agent_job poll (using the FakeCore/FakeBatch pattern
   from test_k8s_jobs.py) completes with status="complete".
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest
from temporalio.testing import ActivityEnvironment

from devloop import k8s_jobs
from devloop.projects import ProjectConfig, _REGISTRY
from devloop.shared import AgentJobResult, DispatchInput, JobStatus, TaskSpec

# --------------------------------------------------------------------------- #
# Shared fixtures / helpers (mirrors test_k8s_jobs.py pattern)
# --------------------------------------------------------------------------- #

_PROJECT = ProjectConfig(
    id="omneval",
    github_url="https://github.com/omneval/omneval",
    default_branch="main",
    agent_image="ghcr.io/example/agent:sha-test",
    agent_label="agent-ready",
    omneval_ingest_secret="omneval-ingest-omneval",
    github_token_secret="omneval-agent-github-token",
)


@pytest.fixture(autouse=True)
def _register_project():
    _REGISTRY.clear()
    _REGISTRY["omneval"] = _PROJECT
    yield
    _REGISTRY.clear()


def _dispatch_input(**kw):
    spec = TaskSpec(
        phase="execute",
        project_id="omneval",
        issue_number=42,
        title="stub test issue",
        body="test",
    )
    return DispatchInput(
        project_id="omneval",
        issue_number=42,
        task_spec=spec,
        poll_interval_seconds=0.0,
        **kw,
    )


def _cm(data):
    return types.SimpleNamespace(data=data)


def _job(succeeded=None, failed=None):
    return types.SimpleNamespace(
        status=types.SimpleNamespace(succeeded=succeeded, failed=failed)
    )


class FakeBatch:
    def __init__(self, job_states):
        self._states = list(job_states)
        self.created = []

    def create_namespaced_job(self, ns, body):
        self.created.append((ns, body))

    def read_namespaced_job_status(self, name, ns):
        state = self._states.pop(0) if len(self._states) > 1 else self._states[0]
        return _job(*state)


class FakeCore:
    def __init__(self, cm_payloads):
        self._payloads = list(cm_payloads)

    def read_namespaced_config_map(self, name, ns):
        from kubernetes.client.exceptions import ApiException

        payload = (
            self._payloads.pop(0) if len(self._payloads) > 1 else self._payloads[0]
        )
        if payload is None:
            raise ApiException(status=404, reason="Not Found")
        return _cm({"result": json.dumps(payload)})


def _patch(monkeypatch, batch, core):
    monkeypatch.setattr(k8s_jobs.cluster, "batch", lambda: batch)
    monkeypatch.setattr(k8s_jobs.cluster, "core", lambda: core)


# --------------------------------------------------------------------------- #
# Entrypoint importer (mirrors agents/images/base import pattern)
# --------------------------------------------------------------------------- #

_ENTRYPOINT_PATH = (
    Path(__file__).parent.parent / "images" / "agent-base" / "entrypoint.py"
)


def _load_entrypoint():
    """Import agents/images/base/entrypoint.py by path (different tree from temporal-worker)."""
    # Put the entrypoint's directory on sys.path so its sibling `import skills`
    # resolves — mirroring the container, where `python /usr/local/bin/agent-
    # entrypoint.py` makes that dir sys.path[0]. Without this the by-path load
    # can't see skills.py and ModuleNotFoundError surfaces here, not in prod.
    base_dir = str(_ENTRYPOINT_PATH.parent)
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)
    spec = importlib.util.spec_from_file_location("entrypoint", _ENTRYPOINT_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Don't pollute sys.modules permanently — use a unique key so each reload is fresh.
    sys.modules.setdefault("entrypoint", mod)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# Cycle 1 — render_job propagates AGENT_STUB when set
# --------------------------------------------------------------------------- #


def test_render_job_propagates_agent_stub_when_set(monkeypatch):
    """When AGENT_STUB=1 is in the worker env, render_job must forward it into
    the Job container env so the agent entrypoint takes the stub fast-path."""
    monkeypatch.setenv("AGENT_STUB", "1")

    d = _dispatch_input()
    manifest = k8s_jobs.render_job(d, "agent-omneval-execute-42-a1")
    env = {
        e["name"]: e.get("value")
        for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
    }

    assert "AGENT_STUB" in env, "AGENT_STUB must be present in the Job container env"
    assert env["AGENT_STUB"] == "1"


def test_render_job_omits_agent_stub_when_not_set(monkeypatch):
    """When AGENT_STUB is absent from the worker env, the Job env must not
    include it — no stale stub that would skip real agent execution in prod."""
    monkeypatch.delenv("AGENT_STUB", raising=False)

    d = _dispatch_input()
    manifest = k8s_jobs.render_job(d, "agent-omneval-execute-42-a1")
    env_names = {
        e["name"] for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
    }

    assert "AGENT_STUB" not in env_names


# --------------------------------------------------------------------------- #
# Cycle 2 — entrypoint stub path produces a parseable output payload
# --------------------------------------------------------------------------- #


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def origin(tmp_path):
    """A bare 'remote' repo with one commit on main (same pattern as test_entrypoint.py)."""
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


def test_entrypoint_stub_produces_valid_output_payload(origin, tmp_path, monkeypatch):
    """AGENT_STUB=1 entrypoint run writes a JSON payload parseable by
    AgentJobResult.from_payload.

    Git/clone/push/pr helpers run for real against the local bare repo; only
    open_draft_pr is stubbed (no gh auth needed).  The payload must have a
    terminal status and all fields AgentJobResult.from_payload consumes.
    """
    entrypoint = _load_entrypoint()

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
                "issue_number": 42,
                "title": "stub test issue",
            }
        ),
    )
    monkeypatch.setenv("GITHUB_URL", str(origin))
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    monkeypatch.setenv("WORKDIR", str(workdir))
    monkeypatch.setenv("OUTPUT_CONFIGMAP", "agent-omneval-execute-42-a1")
    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    rc = entrypoint.main()
    assert rc == 0, "entrypoint must exit 0 in stub mode"

    payload = json.loads(out_file.read_text())

    # Terminal status
    terminal_statuses = {JobStatus.COMPLETE.value, JobStatus.FAILED.value}
    assert payload.get("status") in terminal_statuses, (
        f"payload status must be terminal; got {payload.get('status')!r}"
    )

    # Fields consumed by AgentJobResult.from_payload
    for field_name in ("status", "issue_number", "branch", "pr_url", "tests_passed"):
        assert field_name in payload, f"payload missing field: {field_name!r}"


# --------------------------------------------------------------------------- #
# Cycle 3 — AgentJobResult.from_payload returns terminal AgentJobResult
# --------------------------------------------------------------------------- #


def test_result_from_payload_returns_terminal_result(origin, tmp_path, monkeypatch):
    """The payload produced by the stub path feeds through
    AgentJobResult.from_payload to yield a terminal AgentJobResult — the
    workflow advances."""
    entrypoint = _load_entrypoint()

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
                "issue_number": 42,
                "title": "stub test issue",
            }
        ),
    )
    monkeypatch.setenv("GITHUB_URL", str(origin))
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    monkeypatch.setenv("WORKDIR", str(workdir))
    monkeypatch.setenv("OUTPUT_CONFIGMAP", "agent-omneval-execute-42-a1")
    monkeypatch.setenv("OUTPUT_FILE", str(out_file))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    entrypoint.main()
    payload = json.loads(out_file.read_text())

    # Feed through the worker's result parser — this is what dispatch_agent_job reads
    result = AgentJobResult.from_payload(payload, "agent-omneval-execute-42-a1")

    assert isinstance(result, AgentJobResult)
    assert result.status in {JobStatus.COMPLETE.value, JobStatus.FAILED.value}
    assert result.job_name == "agent-omneval-execute-42-a1"


# --------------------------------------------------------------------------- #
# Cycle 4 — fake-k8s dispatch poll using stub payload completes the activity
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dispatch_with_stub_payload_returns_complete(monkeypatch):
    """A fake-k8s dispatch_agent_job poll where the ConfigMap contains the
    exact payload shape the stub entrypoint produces returns a terminal
    AgentJobResult with status=complete — workflow would advance."""
    stub_payload = {
        "status": "complete",
        "issue_number": 42,
        "branch": "agent/issue-42",
        "pr_url": "pr://stub",
        "tests_passed": False,  # stub doesn't run real tests
        "summary": "stub run\n--- test output ---\nno tests detected — skipped",
    }

    batch = FakeBatch([(1, None)])  # job succeeded immediately
    core = FakeCore([stub_payload])
    _patch(monkeypatch, batch, core)

    result = await ActivityEnvironment().run(
        k8s_jobs.dispatch_agent_job, _dispatch_input()
    )

    assert result.status == JobStatus.COMPLETE.value
    assert result.branch == "agent/issue-42"
    assert result.pr_url == "pr://stub"
    assert result.issue_number == 42
    assert batch.created, "Job must have been created"


# --------------------------------------------------------------------------- #
# Cycle 5 — the protocol owns its shape: round-trips are symmetric
# --------------------------------------------------------------------------- #


def test_agentjobresult_payload_round_trips():
    """to_payload → from_payload reconstructs the result (job_name is assigned by
    the reader, not carried on the wire)."""
    r = AgentJobResult(
        status=JobStatus.COMPLETE.value,
        issue_number=42,
        branch="agent/issue-42",
        pr_url="pr://x",
        commits=3,
        tests_passed=True,
        review={"summary": "ok", "inline_comments": []},
        summary="done",
    )
    back = AgentJobResult.from_payload(r.to_payload(), "job-1")
    assert "job_name" not in r.to_payload()
    assert back == AgentJobResult(**{**r.__dict__, "job_name": "job-1"})


def test_taskspec_env_round_trips():
    spec = TaskSpec(
        phase="merge",
        project_id="omneval",
        issue_number=7,
        extra={"branches": ["agent/issue-7"]},
    )
    assert TaskSpec.from_env(spec.to_env_value()) == spec
