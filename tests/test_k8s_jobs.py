"""Unit tests for the Agent Execution Job dispatch activity (issue #18).

Uses a fake kubernetes client (no cluster) and Temporal's ActivityEnvironment
so ``activity.info().attempt`` resolves.
"""

import json
import types

import pytest
from kubernetes.client.exceptions import ApiException
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from datetime import timedelta

from devloop import k8s_jobs
from devloop._constants import _ACTIVITY_TIMEOUT
from devloop.projects import ProjectConfig, _REGISTRY
from devloop.shared import DispatchInput, JobStatus, TaskSpec

_PROJECT = ProjectConfig(
    id="omneval",
    github_url="https://github.com/omneval/omneval",
    default_branch="main",
    agent_image="zacharybloss/agent-omneval:sha-test",
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


def _cm(data):
    return types.SimpleNamespace(data=data)


def _job(succeeded=None, failed=None):
    return types.SimpleNamespace(
        status=types.SimpleNamespace(succeeded=succeeded, failed=failed)
    )


def _not_found():
    return ApiException(status=404, reason="Not Found")


class FakeBatch:
    def __init__(self, job_states):
        # job_states: list of (succeeded, failed) tuples returned in order
        self._states = list(job_states)
        self.created = []
        self.deleted = []

    def create_namespaced_job(self, ns, body):
        self.created.append((ns, body))

    def read_namespaced_job_status(self, name, ns):
        state = self._states.pop(0) if len(self._states) > 1 else self._states[0]
        return _job(*state)

    def delete_namespaced_job(self, name, ns, body=None):
        self.deleted.append(name)


class FakeCore:
    def __init__(self, cm_payloads):
        # cm_payloads: list of dicts (or None for 404) returned in order
        self._payloads = list(cm_payloads)
        self.patched = []
        self.deleted = []

    def read_namespaced_config_map(self, name, ns):
        payload = (
            self._payloads.pop(0) if len(self._payloads) > 1 else self._payloads[0]
        )
        if payload is None:
            raise _not_found()
        return _cm({"result": json.dumps(payload)})

    def patch_namespaced_config_map(self, name, ns, body):
        self.patched.append((name, body))

    def delete_namespaced_config_map(self, name, ns):
        self.deleted.append(name)


def _patch(monkeypatch, batch, core):
    monkeypatch.setattr(k8s_jobs.cluster, "batch", lambda: batch)
    monkeypatch.setattr(k8s_jobs.cluster, "core", lambda: core)


def _dispatch_input(**kw):
    spec = TaskSpec(
        phase="execute",
        project_id="omneval",
        issue_number=42,
        title="Add feature",
        body="do the thing",
    )
    return DispatchInput(
        project_id="omneval",
        issue_number=42,
        task_spec=spec,
        poll_interval_seconds=0.0,
        **kw,
    )


# --------------------------------------------------------------------------- #
# render_job
# --------------------------------------------------------------------------- #
def test_render_job_sets_otlp_and_secret_env(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    d = _dispatch_input()
    manifest = k8s_jobs.render_job(d, "agent-omneval-execute-42-a1")
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in container["env"]}

    assert container["image"] == "zacharybloss/agent-omneval:sha-test"
    assert env["OTEL_SERVICE_NAME"]["value"] == "execute"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"]["value"] == "http/protobuf"
    assert env["OTEL_EXPORTER_OTLP_HEADERS"]["value"] == "x-api-key=$(OMNEVAL_API_KEY)"
    # X-API-Key sourced from the project's ingest secret, not a bearer token
    assert (
        env["OMNEVAL_API_KEY"]["valueFrom"]["secretKeyRef"]["name"]
        == "omneval-ingest-omneval"
    )
    # GitHub token comes from the project's own scoped secret (per-org)
    assert (
        env["GITHUB_TOKEN"]["valueFrom"]["secretKeyRef"]["name"]
        == "omneval-agent-github-token"
    )
    # GH_TOKEN mirrors GITHUB_TOKEN so the ``gh`` CLI can authenticate inside
    # the OpenHands sandbox (env vars are inherited from the container).
    assert (
        env["GH_TOKEN"]["valueFrom"]["secretKeyRef"]["name"]
        == "omneval-agent-github-token"
    )
    # task spec is serialized for the entrypoint
    assert json.loads(env["TASK_SPEC"]["value"])["issue_number"] == 42
    assert manifest["spec"]["backoffLimit"] == 0
    assert manifest["metadata"]["namespace"] == k8s_jobs.NAMESPACE


def test_job_name_uses_issue_number_when_present():
    spec = TaskSpec(phase="execute", project_id="omneval", issue_number=42)
    d = DispatchInput(project_id="omneval", issue_number=42, task_spec=spec)
    assert k8s_jobs.job_name_for(d, 1) == "agent-omneval-execute-42-a1"


def test_job_name_uses_discriminator_when_no_issue():
    """Alert diagnosis jobs (issue_number 0) must get a per-workflow discriminator
    so concurrent alerts don't collide on one Job name + output ConfigMap."""
    spec = TaskSpec(phase="diagnosis", project_id="homelab-alerts")
    d = DispatchInput(project_id="homelab-alerts", issue_number=0, task_spec=spec)
    a = k8s_jobs.job_name_for(d, 1, discriminator="abc12345")
    b = k8s_jobs.job_name_for(d, 1, discriminator="def67890")
    assert a == "agent-homelab-alerts-diagnosis-abc12345-a1"
    assert a != b  # different workflows → different Job names


def test_render_job_defaults_to_agent_job_sa():
    d = _dispatch_input()
    manifest = k8s_jobs.render_job(d, "agent-omneval-execute-42-a1")
    assert (
        manifest["spec"]["template"]["spec"]["serviceAccountName"]
        == k8s_jobs.SERVICE_ACCOUNT
    )


def test_render_job_honors_service_account_override():
    """Alert Response diagnosis jobs run as a read-only cluster SA so the agent
    can inspect the cluster; render_job must use the override when set."""
    d = _dispatch_input(service_account_override="agent-diagnosis")
    manifest = k8s_jobs.render_job(d, "agent-homelab-alerts-diagnosis-a1")
    assert (
        manifest["spec"]["template"]["spec"]["serviceAccountName"] == "agent-diagnosis"
    )


def test_render_job_omits_github_token_when_no_secret():
    # A job with no registry project and no github token override (e.g. an Alert
    # Response diagnosis) must not reference a GitHub token secret.
    spec = TaskSpec(phase="diagnosis", project_id="unknown", issue_number=0)
    d = DispatchInput(
        project_id="unknown",
        issue_number=0,
        task_spec=spec,
        poll_interval_seconds=0.0,
        omneval_secret_override="omneval-ingest-homelab-alerts",
    )
    manifest = k8s_jobs.render_job(d, "agent-unknown-diagnosis-a1")
    env = {
        e["name"]: e
        for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env


def test_job_name_includes_attempt():
    d = _dispatch_input()
    assert k8s_jobs.job_name_for(d, 1).endswith("-a1")
    assert k8s_jobs.job_name_for(d, 3).endswith("-a3")


# --------------------------------------------------------------------------- #
# dispatch_agent_job
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_dispatch_completes_and_reads_configmap(monkeypatch):
    batch = FakeBatch([(None, None), (1, None)])  # pending then succeeded
    core = FakeCore(
        [
            None,
            {
                "status": "complete",
                "branch": "agent/issue-42",
                "pr_url": "https://github.com/omneval/omneval/pull/9",
                "tests_passed": True,
                "issue_number": 42,
            },
        ]
    )
    _patch(monkeypatch, batch, core)

    result = await ActivityEnvironment().run(
        k8s_jobs.dispatch_agent_job, _dispatch_input()
    )

    assert result.status == JobStatus.COMPLETE.value
    assert result.branch == "agent/issue-42"
    assert result.pr_url.endswith("/pull/9")
    assert result.tests_passed is True
    assert batch.created, "a Job should have been created"


@pytest.mark.asyncio
async def test_dispatch_raises_on_job_failure_for_temporal_retry(monkeypatch):
    batch = FakeBatch([(None, 1)])  # failed immediately
    core = FakeCore([{"status": "failed", "error": "boom"}])
    _patch(monkeypatch, batch, core)

    with pytest.raises(ApplicationError):
        await ActivityEnvironment().run(k8s_jobs.dispatch_agent_job, _dispatch_input())


@pytest.mark.asyncio
async def test_dispatch_returns_awaiting_human(monkeypatch):
    batch = FakeBatch([(None, None)])  # still running
    core = FakeCore([{"status": "awaiting_human", "question": "Use lib A or B?"}])
    _patch(monkeypatch, batch, core)

    result = await ActivityEnvironment().run(
        k8s_jobs.dispatch_agent_job, _dispatch_input()
    )

    assert result.status == JobStatus.AWAITING_HUMAN.value
    assert result.question == "Use lib A or B?"
    assert batch.deleted == [], "job must NOT be deleted while awaiting a human"


@pytest.mark.asyncio
async def test_dispatch_attaches_to_existing_job_on_conflict(monkeypatch):
    class ConflictBatch(FakeBatch):
        def create_namespaced_job(self, ns, body):
            raise ApiException(status=409, reason="Conflict")

    batch = ConflictBatch([(1, None)])
    core = FakeCore([{"status": "complete", "branch": "b"}])
    _patch(monkeypatch, batch, core)

    result = await ActivityEnvironment().run(
        k8s_jobs.dispatch_agent_job, _dispatch_input()
    )
    assert result.status == JobStatus.COMPLETE.value


@pytest.mark.asyncio
async def test_cleanup_deletes_configmap_only(monkeypatch):
    batch = FakeBatch([(1, None)])
    core = FakeCore([None])
    _patch(monkeypatch, batch, core)

    await ActivityEnvironment().run(
        k8s_jobs.cleanup_configmap, "agent-omneval-execute-42-a1"
    )
    assert core.deleted == ["agent-omneval-execute-42-a1"]
    assert batch.deleted == [], (
        "Job deletion should be left to K8s ttlSecondsAfterFinished"
    )


# --------------------------------------------------------------------------- #
# Fix #34 — LLM + OTLP env pass-through into Job manifest
# --------------------------------------------------------------------------- #


def test_render_job_passes_llm_env_when_set(monkeypatch):
    """When AGENT_MODEL, AGENT_LLM_BASE_URL, AGENT_LLM_API_KEY are set in the
    worker environment, render_job must forward them into the Job container env
    so the agent entrypoint can reach the DGX model endpoint."""
    monkeypatch.setenv("AGENT_MODEL", "qwen3.6-27b-mtp")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", "http://dgx.local/v1")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "secret-key-42")

    d = _dispatch_input()
    manifest = k8s_jobs.render_job(d, "agent-omneval-execute-42-a1")
    env = {
        e["name"]: e["value"]
        for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in e
    }

    assert env["AGENT_MODEL"] == "qwen3.6-27b-mtp"
    assert env["AGENT_LLM_BASE_URL"] == "http://dgx.local/v1"
    assert env["AGENT_LLM_API_KEY"] == "secret-key-42"


def test_render_job_passes_per_role_llm_env_when_set(monkeypatch):
    """Per-role LLM overrides (review/audit/extract) must be forwarded into the
    Job container env so the entrypoint can route the Review phase, the
    criteria audit, and structured extraction to a different model."""
    monkeypatch.setenv("AGENT_MODEL", "openai/qwen3.6-27b-mtp")
    monkeypatch.setenv("AGENT_MODEL_REVIEW", "anthropic/claude-sonnet-4-6")
    monkeypatch.setenv("AGENT_LLM_BASE_URL_REVIEW", "https://api.anthropic.com/v1/")
    monkeypatch.setenv("AGENT_LLM_API_KEY_REVIEW", "sk-ant-test")
    monkeypatch.setenv("AGENT_MODEL_AUDIT", "anthropic/claude-haiku-4-5")
    monkeypatch.delenv("AGENT_MODEL_EXTRACT", raising=False)

    d = _dispatch_input()
    manifest = k8s_jobs.render_job(d, "agent-omneval-execute-42-a1")
    env = {
        e["name"]: e["value"]
        for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in e
    }

    assert env["AGENT_MODEL_REVIEW"] == "anthropic/claude-sonnet-4-6"
    assert env["AGENT_LLM_BASE_URL_REVIEW"] == "https://api.anthropic.com/v1/"
    assert env["AGENT_LLM_API_KEY_REVIEW"] == "sk-ant-test"
    assert env["AGENT_MODEL_AUDIT"] == "anthropic/claude-haiku-4-5"
    assert "AGENT_MODEL_EXTRACT" not in env  # unset roles are not forwarded


def test_render_job_falls_back_to_default_image_when_agent_image_empty(monkeypatch):
    """A registry project without agent_image runs on AGENT_DEFAULT_IMAGE (the
    published devloop-agent-universal) — enrolling a project must not require
    building a per-project image."""
    _REGISTRY["omneval"] = ProjectConfig(
        id="omneval",
        github_url="https://github.com/omneval/omneval",
        default_branch="main",
        agent_label="agent-ready",
        omneval_ingest_secret="omneval-ingest-omneval",
        github_token_secret="omneval-agent-github-token",
    )
    monkeypatch.setattr(
        k8s_jobs, "AGENT_DEFAULT_IMAGE", "ghcr.io/omneval/devloop-agent-universal:9.9.9"
    )

    d = _dispatch_input()
    manifest = k8s_jobs.render_job(d, "agent-omneval-execute-42-a1")
    image = manifest["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image == "ghcr.io/omneval/devloop-agent-universal:9.9.9"


def test_render_job_explicit_agent_image_still_wins(monkeypatch):
    """A registry entry that sets agent_image keeps using it untouched."""
    monkeypatch.setattr(
        k8s_jobs, "AGENT_DEFAULT_IMAGE", "ghcr.io/omneval/devloop-agent-universal:9.9.9"
    )
    d = _dispatch_input()
    manifest = k8s_jobs.render_job(d, "agent-omneval-execute-42-a1")
    image = manifest["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image == _PROJECT.agent_image


def test_render_job_omits_llm_env_when_unset(monkeypatch):
    """LLM vars that are absent from the worker env must not appear in the
    Job container env at all — no empty-value stubs that confuse the entrypoint."""
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("AGENT_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("AGENT_LLM_API_KEY", raising=False)

    d = _dispatch_input()
    manifest = k8s_jobs.render_job(d, "agent-omneval-execute-42-a1")
    env_names = {
        e["name"] for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
    }

    assert "AGENT_MODEL" not in env_names
    assert "AGENT_LLM_BASE_URL" not in env_names
    assert "AGENT_LLM_API_KEY" not in env_names


def test_render_job_passes_agents_namespace_when_set(monkeypatch):
    """When the worker is deployed with AGENTS_NAMESPACE set to a non-default
    namespace (e.g. a parallel test release in "devloop-test"), spawned Jobs
    must inherit it — otherwise their write_output/cluster helpers default to
    "agents" and get 403 Forbidden from their namespace-scoped ServiceAccount
    (caught in real-cluster testing of the github-webook-refactor branch)."""
    monkeypatch.setenv("AGENTS_NAMESPACE", "devloop-test")

    d = _dispatch_input()
    manifest = k8s_jobs.render_job(d, "agent-omneval-execute-42-a1")
    env = {
        e["name"]: e["value"]
        for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in e
    }

    assert env["AGENTS_NAMESPACE"] == "devloop-test"


def test_render_job_omits_agents_namespace_when_unset(monkeypatch):
    """AGENTS_NAMESPACE absent from the worker env must not appear in the Job
    container env — the entrypoint's own os.getenv(..., "agents") fallback
    then matches the worker's NAMESPACE default."""
    monkeypatch.delenv("AGENTS_NAMESPACE", raising=False)

    d = _dispatch_input()
    manifest = k8s_jobs.render_job(d, "agent-omneval-execute-42-a1")
    env_names = {
        e["name"] for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
    }

    assert "AGENTS_NAMESPACE" not in env_names


def test_render_job_passes_otlp_overrides_from_env(monkeypatch):
    """OTEL_EXPORTER_OTLP_ENDPOINT and OTEL_EXPORTER_OTLP_HEADERS, when set
    explicitly in the worker env, must override the hard-coded defaults so
    agent spans export to the correct collector."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://custom-collector:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=custom-key")

    d = _dispatch_input()
    manifest = k8s_jobs.render_job(d, "agent-omneval-execute-42-a1")
    env = {
        e["name"]: e
        for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in e
    }

    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"] == "http://custom-collector:4318"
    assert env["OTEL_EXPORTER_OTLP_HEADERS"]["value"] == "x-api-key=custom-key"


def test_render_job_does_not_inject_openai_base_url():
    """OPENAI_BASE_URL must not appear in the job manifest — it was a dead
    injection that no consumer read; AGENT_LLM_BASE_URL is the canonical var."""
    d = _dispatch_input()
    manifest = k8s_jobs.render_job(d, "agent-omneval-execute-42-a1")
    env_names = {
        e["name"] for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert "OPENAI_BASE_URL" not in env_names


def test_activity_timeout_is_deadline_plus_90s_buffer():
    """_ACTIVITY_TIMEOUT must exceed JOB_ACTIVE_DEADLINE by exactly 90 seconds.
    This buffer ensures Temporal always outlasts the K8s pod and can detect
    failure cleanly, preventing a runaway job when the workflow gives up first."""
    assert _ACTIVITY_TIMEOUT == timedelta(seconds=k8s_jobs.JOB_ACTIVE_DEADLINE + 90)


def test_render_job_sets_active_deadline_from_job_active_deadline(monkeypatch):
    """render_job must set activeDeadlineSeconds from JOB_ACTIVE_DEADLINE so that
    changing AGENT_JOB_ACTIVE_DEADLINE (via helm maxAgentRuntime) caps the pod
    lifetime and prevents the K8s job from outliving the Temporal workflow."""
    monkeypatch.setattr(k8s_jobs, "JOB_ACTIVE_DEADLINE", 10800)
    manifest = k8s_jobs.render_job(_dispatch_input(), "agent-omneval-execute-42-a1")
    assert manifest["spec"]["activeDeadlineSeconds"] == 10800


def test_render_job_does_not_set_otel_service_name_from_env(monkeypatch):
    """OTEL_SERVICE_NAME must be set by the Job manifest to spec.phase, NOT
    inherited from the worker env — the entrypoint tags spans per-phase."""
    monkeypatch.setenv("OTEL_SERVICE_NAME", "some-worker-name")

    d = _dispatch_input()
    manifest = k8s_jobs.render_job(d, "agent-omneval-execute-42-a1")
    env = {
        e["name"]: e["value"]
        for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in e
    }

    # Must be the phase, not the worker's own service name
    assert env["OTEL_SERVICE_NAME"] == "execute"


# --------------------------------------------------------------------------- #
# Per-phase skill enablement (issue #36)
# --------------------------------------------------------------------------- #


def _job_env(d=None, job_name="agent-omneval-execute-42-a1"):
    """Helper: render and return a {name: value} env dict (value-only entries)."""
    if d is None:
        d = _dispatch_input()
    manifest = k8s_jobs.render_job(d, job_name)
    return {
        e["name"]: e["value"]
        for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in e
    }


def _job_env_names(d=None, job_name="agent-omneval-execute-42-a1"):
    """Helper: render and return a set of all env var names in the Job."""
    if d is None:
        d = _dispatch_input()
    manifest = k8s_jobs.render_job(d, job_name)
    return {
        e["name"] for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
    }


def test_render_job_injects_skills_enabled_for_named_phase(monkeypatch):
    """When AGENT_SKILLS_BY_PHASE has names for the active phase, render_job
    must inject AGENT_SKILLS_ENABLED as a comma-separated list into the Job."""
    monkeypatch.setenv(
        "AGENT_SKILLS_BY_PHASE",
        json.dumps({"execute": ["tdd", "code-review"], "review": ["code-review"]}),
    )
    env = _job_env()
    assert env["AGENT_SKILLS_ENABLED"] == "tdd,code-review"


def test_render_job_injects_empty_skills_enabled_for_empty_phase(monkeypatch):
    """When the active phase maps to [] (no skills), render_job must inject
    AGENT_SKILLS_ENABLED="" so the entrypoint knows to allow no skills."""
    monkeypatch.setenv(
        "AGENT_SKILLS_BY_PHASE",
        json.dumps({"execute": [], "review": ["code-review"]}),
    )
    env = _job_env()
    assert "AGENT_SKILLS_ENABLED" in env
    assert env["AGENT_SKILLS_ENABLED"] == ""


def test_render_job_omits_skills_enabled_when_phase_absent_from_map(monkeypatch):
    """When the active phase is absent from AGENT_SKILLS_BY_PHASE, render_job
    must NOT inject AGENT_SKILLS_ENABLED — the entrypoint then allows all skills
    (backward-compatible default)."""
    monkeypatch.setenv(
        "AGENT_SKILLS_BY_PHASE",
        json.dumps({"review": ["code-review"]}),  # "execute" is absent
    )
    env_names = _job_env_names()
    assert "AGENT_SKILLS_ENABLED" not in env_names


def test_render_job_omits_skills_enabled_when_by_phase_unset(monkeypatch):
    """When AGENT_SKILLS_BY_PHASE is not set at all (existing deployments),
    AGENT_SKILLS_ENABLED must be absent from the Job env — all skills allowed."""
    monkeypatch.delenv("AGENT_SKILLS_BY_PHASE", raising=False)
    env_names = _job_env_names()
    assert "AGENT_SKILLS_ENABLED" not in env_names


def test_render_job_injects_selection_mode_from_env(monkeypatch):
    """AGENT_SKILLS_SELECTION_MODE from the worker env must be forwarded to
    the Job so the entrypoint can apply the right skill-selection strategy."""
    monkeypatch.setenv("AGENT_SKILLS_SELECTION_MODE", "advanced")
    env = _job_env()
    assert env["AGENT_SKILLS_SELECTION_MODE"] == "advanced"


def test_render_job_defaults_selection_mode_to_triggers(monkeypatch):
    """When AGENT_SKILLS_SELECTION_MODE is not set, render_job must default
    to 'triggers' to preserve backward-compatible keyword-driven behaviour."""
    monkeypatch.delenv("AGENT_SKILLS_SELECTION_MODE", raising=False)
    env = _job_env()
    assert env["AGENT_SKILLS_SELECTION_MODE"] == "triggers"


def test_render_job_applies_cpu_and_memory_limits_from_env(monkeypatch):
    """AGENT_JOB_CPU_LIMIT and AGENT_JOB_MEMORY_LIMIT, when set, must appear in
    the Job container's resource limits so the agent pod is properly bounded."""
    monkeypatch.setenv("AGENT_JOB_CPU_LIMIT", "1")
    monkeypatch.setenv("AGENT_JOB_MEMORY_LIMIT", "3Gi")

    d = _dispatch_input()
    manifest = k8s_jobs.render_job(d, "agent-omneval-execute-42-a1")
    resources = manifest["spec"]["template"]["spec"]["containers"][0]["resources"]

    assert resources["limits"]["cpu"] == "1"
    assert resources["limits"]["memory"] == "3Gi"


def test_render_job_omits_cpu_limit_when_unset(monkeypatch):
    """When AGENT_JOB_CPU_LIMIT is absent the Job must have no cpu limit — a CPU
    limit on a bursty agent process would cause throttling under peak LLM calls."""
    monkeypatch.delenv("AGENT_JOB_CPU_LIMIT", raising=False)
    monkeypatch.delenv("AGENT_JOB_MEMORY_LIMIT", raising=False)

    d = _dispatch_input()
    manifest = k8s_jobs.render_job(d, "agent-omneval-execute-42-a1")
    resources = manifest["spec"]["template"]["spec"]["containers"][0]["resources"]

    assert "cpu" not in resources["limits"]


def test_render_job_memory_limit_defaults_to_request_when_unset(monkeypatch):
    """When AGENT_JOB_MEMORY_LIMIT is absent, the memory limit must fall back to
    the memory request so the pod has a bounded memory envelope."""
    monkeypatch.setenv("AGENT_JOB_MEMORY", "2Gi")
    monkeypatch.delenv("AGENT_JOB_MEMORY_LIMIT", raising=False)

    d = _dispatch_input()
    manifest = k8s_jobs.render_job(d, "agent-omneval-execute-42-a1")
    resources = manifest["spec"]["template"]["spec"]["containers"][0]["resources"]

    assert resources["limits"]["memory"] == "2Gi"


def test_render_job_handles_invalid_by_phase_json_gracefully(monkeypatch):
    """If AGENT_SKILLS_BY_PHASE is not valid JSON, render_job must not crash
    and must omit AGENT_SKILLS_ENABLED (treat as absent → all skills)."""
    monkeypatch.setenv("AGENT_SKILLS_BY_PHASE", "not-valid-json")
    # Must not raise
    env_names = _job_env_names()
    assert "AGENT_SKILLS_ENABLED" not in env_names


# --------------------------------------------------------------------------- #
# ConfigMap skills delivery (issue #34)
# --------------------------------------------------------------------------- #


def _job_manifest(d=None, job_name="agent-omneval-execute-42-a1"):
    """Helper: render and return the full Job manifest."""
    if d is None:
        d = _dispatch_input()
    return k8s_jobs.render_job(d, job_name)


def test_render_job_forwards_agent_skills_configmap_env(monkeypatch):
    """When AGENT_SKILLS_CONFIGMAP is set, render_job must forward it to the
    Job so the entrypoint knows which ConfigMap to stage."""
    monkeypatch.setenv("AGENT_SKILLS_CONFIGMAP", "my-release-skills")
    env = _job_env()
    assert env["AGENT_SKILLS_CONFIGMAP"] == "my-release-skills"


def test_render_job_adds_skills_volume_and_mount(monkeypatch):
    """When AGENT_SKILLS_CONFIGMAP is set, render_job must add a ConfigMap
    volume and a read-only volumeMount at the skills staging path."""
    monkeypatch.setenv("AGENT_SKILLS_CONFIGMAP", "my-release-skills")
    manifest = _job_manifest()
    pod_spec = manifest["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]

    # Volume
    volumes = {v["name"]: v for v in pod_spec.get("volumes", [])}
    assert "skills-configmap" in volumes
    assert volumes["skills-configmap"]["configMap"]["name"] == "my-release-skills"

    # VolumeMount
    mounts = {m["name"]: m for m in container.get("volumeMounts", [])}
    assert "skills-configmap" in mounts
    assert mounts["skills-configmap"]["mountPath"] == "/etc/agent-skills/staging"
    assert mounts["skills-configmap"]["readOnly"] is True


def test_render_job_omits_skills_volume_when_configmap_unset(monkeypatch):
    """When AGENT_SKILLS_CONFIGMAP is not set, render_job must NOT add any
    ConfigMap volume, volumeMount, or env — the manifest is unchanged."""
    monkeypatch.delenv("AGENT_SKILLS_CONFIGMAP", raising=False)
    manifest = _job_manifest()
    pod_spec = manifest["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]

    vol_names = {v["name"] for v in pod_spec.get("volumes", [])}
    mount_names = {m["name"] for m in container.get("volumeMounts", [])}
    env_names = _job_env_names()

    assert "skills-configmap" not in vol_names
    assert "skills-configmap" not in mount_names
    assert "AGENT_SKILLS_CONFIGMAP" not in env_names
