"""Kubernetes Job dispatch for Agent Execution Jobs (issue #18).

A single Temporal activity, ``dispatch_agent_job``, renders a ``batch/v1`` Job
from a Project Registry entry, creates it in the namespace given by
``AGENTS_NAMESPACE`` (defaulting to the Helm release namespace via the chart,
or ``"agents"`` in local/dev runs without a chart), polls it to a terminal
state, and reads the output ConfigMap the Job writes.

Design notes
------------
* The kubernetes client is reached through the ``cluster`` module
  (``cluster.batch()`` / ``cluster.core()`` and its ConfigMap helpers) so unit
  tests can monkeypatch one seam without a cluster.
* A failed Job (or a Job whose output ConfigMap reports ``failed``) raises an
  exception so Temporal's retry policy (max 3) re-runs the activity. Each
  attempt gets a fresh Job name (``…-a<attempt>``).
* If the Job reports ``awaiting_human`` (a mid-run blocking question, issue
  #21) the activity returns that result *without* deleting the Job — it stays
  Running, polling its input ConfigMap for the answer. The workflow then calls
  :func:`answer_agent_job` and :func:`await_agent_job`.
* ``cleanup_configmap`` deletes the output ConfigMap once the workflow has consumed
  the result. The Job is cleaned up natively by Kubernetes via ``ttlSecondsAfterFinished``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os

from temporalio import activity
from temporalio.exceptions import ApplicationError

from . import cluster
from .cluster import NAMESPACE
from .shared import (
    KEY_HUMAN_ANSWER,
    KEY_RESULT,
    AgentJobResult,
    AnswerInput,
    AwaitInput,
    DispatchInput,
    JobStatus,
)
from .projects import get_project

log = logging.getLogger(__name__)

SERVICE_ACCOUNT = os.getenv("AGENT_JOB_SERVICE_ACCOUNT", "agent-job")
TEMPORAL_HOST = os.getenv("TEMPORAL_HOST", "localhost:7233")
OMNEVAL_OTLP_ENDPOINT = os.getenv(
    "OMNEVAL_OTLP_ENDPOINT", "http://omneval-ingest.omneval.svc.cluster.local:8000"
)
AGENT_BASE_IMAGE = os.getenv(
    "AGENT_BASE_IMAGE", "ghcr.io/omneval/devloop-agent-base:latest"
)
# Image used for a registry project whose entry omits agent_image: the
# published batteries-included toolchain image (Go/Node/Helm on top of
# agent-base), so enrolling a project does not require building an image.
AGENT_DEFAULT_IMAGE = os.getenv(
    "AGENT_DEFAULT_IMAGE", "ghcr.io/omneval/devloop-agent-universal:latest"
)

JOB_ACTIVE_DEADLINE = int(os.getenv("AGENT_JOB_ACTIVE_DEADLINE", "7200"))

# Mount path where ConfigMap skills are staged for the entrypoint to install.
SKILLS_STAGING_DIR = "/etc/agent-skills/staging"


# --------------------------------------------------------------------------- #
# Job spec rendering
# --------------------------------------------------------------------------- #
def job_name_for(d: DispatchInput, attempt: int, discriminator: str = "") -> str:
    """Build a Job name. Dev-loop jobs disambiguate by issue number; jobs with
    no issue (Alert Response diagnosis) use ``discriminator`` (a per-workflow
    hash) so two concurrent/rapid alerts don't collide on the same Job name and
    read each other's stale output ConfigMap."""
    spec = d.task_spec
    base = f"agent-{spec.project_id}-{spec.phase}"
    if d.issue_number:
        base += f"-{d.issue_number}"
    elif discriminator:
        base += f"-{discriminator}"
    return f"{base}-a{attempt}".replace("_", "-").lower()[:60]


def _resolve_job_refs(d: DispatchInput):
    """Resolve (image, omneval_secret, github_url, default_branch, github_token_secret).

    Registry-backed jobs read from the project entry; alert-response style jobs
    pass explicit overrides and have no registry project. ``github_token_secret``
    may be empty for jobs that need no GitHub access (e.g. diagnosis).
    """
    try:
        cfg = get_project(d.project_id)
    except KeyError:
        cfg = None
    # Registry project without an explicit agent_image → the universal image;
    # non-registry jobs (alert-response diagnosis) keep the agent-base default.
    image = d.image_override or (cfg.agent_image if cfg else "")
    if not image:
        image = AGENT_DEFAULT_IMAGE if cfg else AGENT_BASE_IMAGE
    omneval_secret = d.omneval_secret_override or (
        cfg.omneval_ingest_secret if cfg else ""
    )
    github_url = d.github_url_override or (cfg.github_url if cfg else "")
    default_branch = cfg.default_branch if cfg else "main"
    github_token_secret = d.github_token_secret_override or (
        cfg.github_token_secret if cfg else ""
    )
    if not omneval_secret:
        raise ValueError(
            f"no omneval ingest secret for project {d.project_id!r} "
            "(set omneval_secret_override or add a registry entry)"
        )
    return image, omneval_secret, github_url, default_branch, github_token_secret


def _job_resources() -> dict:
    cpu_request = os.environ.get("AGENT_JOB_CPU", "2")
    memory_request = os.environ.get("AGENT_JOB_MEMORY", "4Gi")
    cpu_limit = os.environ.get("AGENT_JOB_CPU_LIMIT")
    memory_limit = os.environ.get("AGENT_JOB_MEMORY_LIMIT", memory_request)
    limits: dict = {"memory": memory_limit}
    if cpu_limit:
        limits["cpu"] = cpu_limit
    return {
        "requests": {"cpu": cpu_request, "memory": memory_request},
        "limits": limits,
    }


def render_job(d: DispatchInput, job_name: str) -> dict:
    """Render the ``batch/v1`` Job manifest for an Agent Execution Job."""
    image, omneval_secret, github_url, default_branch, github_token_secret = (
        _resolve_job_refs(d)
    )
    spec = d.task_spec

    # omneval ingest is X-API-Key auth, NOT bearer; the omneval project is
    # resolved server-side from the key, so no project_id is sent.
    env = [
        {"name": "TASK_SPEC", "value": spec.to_env_value()},
        {"name": "PROJECT_ID", "value": d.project_id},
        {"name": "GITHUB_URL", "value": github_url},
        {"name": "DEFAULT_BRANCH", "value": default_branch},
        {"name": "TEMPORAL_HOST", "value": TEMPORAL_HOST},
        {"name": "OUTPUT_CONFIGMAP", "value": job_name},
        # OTLP / omneval tracing
        {"name": "OTEL_EXPORTER_OTLP_PROTOCOL", "value": "http/protobuf"},
        {
            "name": "OTEL_EXPORTER_OTLP_ENDPOINT",
            "value": os.environ.get(
                "OTEL_EXPORTER_OTLP_ENDPOINT", OMNEVAL_OTLP_ENDPOINT
            ),
        },
        {
            "name": "OMNEVAL_API_KEY",
            "valueFrom": {"secretKeyRef": {"name": omneval_secret, "key": "api-key"}},
        },
        {
            "name": "OTEL_EXPORTER_OTLP_HEADERS",
            # If the worker has an explicit override use it; otherwise default to
            # omneval X-API-Key substitution referencing the secret env var.
            "value": os.environ.get(
                "OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=$(OMNEVAL_API_KEY)"
            ),
        },
        # OTEL_SERVICE_NAME is set to the phase so spans are tagged per-phase;
        # the worker's own OTEL_SERVICE_NAME is intentionally NOT inherited here.
        {"name": "OTEL_SERVICE_NAME", "value": spec.phase},
    ]

    # Pass through LLM connection env and stub flag from the worker process so
    # the agent entrypoint can reach the DGX model endpoint and take the stub
    # fast-path when AGENT_STUB=1.  Only include vars that are actually set —
    # skip missing ones rather than forwarding empty strings.
    for var in (
        "AGENT_MODEL",
        "AGENT_LLM_BASE_URL",
        "AGENT_LLM_API_KEY",
        # Per-role LLM overrides (review / audit / extract). Each falls back
        # to the base AGENT_MODEL / AGENT_LLM_BASE_URL / AGENT_LLM_API_KEY in
        # the entrypoint when unset, so forwarding only what's set is correct.
        "AGENT_MODEL_REVIEW",
        "AGENT_LLM_BASE_URL_REVIEW",
        "AGENT_LLM_API_KEY_REVIEW",
        "AGENT_MODEL_AUDIT",
        "AGENT_LLM_BASE_URL_AUDIT",
        "AGENT_LLM_API_KEY_AUDIT",
        "AGENT_MODEL_EXTRACT",
        "AGENT_LLM_BASE_URL_EXTRACT",
        "AGENT_LLM_API_KEY_EXTRACT",
        "AGENT_STUB",
        # Execute-phase acceptance-criteria audit loop: how many extra agent
        # passes the entrypoint may spend addressing unmet criteria (default 2).
        "AGENT_CRITERIA_MAX_PASSES",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        # Forwarded so the spawned Job's write_output/cluster helpers target the
        # same namespace the worker itself was deployed into (issue: jobs in a
        # non-default AGENTS_NAMESPACE were writing result ConfigMaps to the
        # "agents" default and getting 403 Forbidden from their scoped SA).
        "AGENTS_NAMESPACE",
    ):
        val = os.environ.get(var)
        if val:
            env.append({"name": var, "value": val})

    # Per-phase skill enablement (issue #36).
    # AGENT_SKILLS_BY_PHASE is a JSON-encoded {phase: [names]} map set on the
    # worker by the Helm chart.  We extract the names for the active phase and
    # pass them as a comma-separated AGENT_SKILLS_ENABLED to the Job so the
    # entrypoint can build its per-phase allowlist.
    #
    # Three-way semantics preserved through the env transport:
    #   phase absent from map → omit AGENT_SKILLS_ENABLED → all skills (default)
    #   phase = []            → AGENT_SKILLS_ENABLED=""   → no skills
    #   phase = [a,b]         → AGENT_SKILLS_ENABLED="a,b"
    _skills_by_phase_raw = os.environ.get("AGENT_SKILLS_BY_PHASE", "")
    if _skills_by_phase_raw:
        try:
            _skills_by_phase: dict = json.loads(_skills_by_phase_raw)
        except (json.JSONDecodeError, ValueError):
            log.warning(
                "AGENT_SKILLS_BY_PHASE is not valid JSON — ignoring: %r",
                _skills_by_phase_raw,
            )
            _skills_by_phase = {}
        if spec.phase in _skills_by_phase:
            phase_names = _skills_by_phase[spec.phase]
            env.append(
                {
                    "name": "AGENT_SKILLS_ENABLED",
                    "value": ",".join(phase_names) if phase_names else "",
                }
            )

    # Forward the selection mode (triggers/advanced) so the entrypoint can
    # pass it through to resolve_skills.  Always set — defaults to "triggers".
    _selection_mode = os.environ.get("AGENT_SKILLS_SELECTION_MODE", "triggers")
    env.append({"name": "AGENT_SKILLS_SELECTION_MODE", "value": _selection_mode})

    # ConfigMap skills delivery (issue #34). When AGENT_SKILLS_CONFIGMAP is set
    # by the worker (from the Helm chart), forward it to the Job so the
    # entrypoint can stage-and-install the skills at pod start.
    _skills_configmap = os.environ.get("AGENT_SKILLS_CONFIGMAP")
    if _skills_configmap:
        env.append(
            {
                "name": "AGENT_SKILLS_CONFIGMAP",
                "value": _skills_configmap,
            }
        )

    # Reviewer the merge phase tags on the PR it opens (assignee + @-mention).
    # Sourced from the registry; absent for non-registry (alert-response) jobs.
    try:
        reviewer = get_project(d.project_id).pr_reviewer
    except KeyError:
        reviewer = ""
    if reviewer:
        env.append({"name": "PR_REVIEWER", "value": reviewer})

    # Agent runner selection (issue #121, ADR-0011): the project's registry
    # entry wins over the deployment-wide AGENT_RUNNER env (Helm
    # temporalWorker.agentJob.runner). Omitted entirely when neither is set —
    # the entrypoint defaults to the openhands runner.
    try:
        project_runner = get_project(d.project_id).agent_runner
    except KeyError:
        project_runner = ""
    agent_runner = project_runner or os.environ.get("AGENT_RUNNER", "")
    if agent_runner:
        env.append({"name": "AGENT_RUNNER", "value": agent_runner})

    # Per-project GitHub token (scoped to that owner/org). Omitted for jobs that
    # need no GitHub access, e.g. Alert Response diagnosis.
    # ``GITHUB_TOKEN`` is used for git push/clone; ``GH_TOKEN`` is consumed by
    # the ``gh`` CLI (``gh issue list``) inside the agent sandbox.
    if github_token_secret:
        env.extend(
            [
                {
                    "name": "GITHUB_TOKEN",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": github_token_secret,
                            "key": "GITHUB_TOKEN",
                        }
                    },
                },
                {
                    "name": "GH_TOKEN",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": github_token_secret,
                            "key": "GITHUB_TOKEN",
                        }
                    },
                },
            ]
        )

    # ConfigMap skills volume and mount (issue #34). When a ConfigMap-backed
    # skills delivery is configured, mount the ConfigMap read-only at the
    # staging path so the entrypoint can install skills at pod start.
    _pod_volumes: list[dict] = []
    _container_mounts: list[dict] = []
    if _skills_configmap:
        _pod_volumes.append(
            {
                "name": "skills-configmap",
                "configMap": {"name": _skills_configmap},
            }
        )
        _container_mounts.append(
            {
                "name": "skills-configmap",
                "mountPath": SKILLS_STAGING_DIR,
                "readOnly": True,
            }
        )

    pod_spec: dict = {
        "restartPolicy": "Never",
        "serviceAccountName": d.service_account_override or SERVICE_ACCOUNT,
        "containers": [
            {
                "name": "agent",
                "image": image,
                "command": ["python", "/usr/local/bin/agent-entrypoint.py"],
                "env": env,
                "resources": _job_resources(),
            }
        ],
    }
    if _pod_volumes:
        pod_spec["volumes"] = _pod_volumes
    if _container_mounts:
        pod_spec["containers"][0]["volumeMounts"] = _container_mounts

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": NAMESPACE,
            "labels": {
                "app.kubernetes.io/managed-by": "orchestration-worker",
                "agents.homelab/project": d.project_id,
                "agents.homelab/phase": spec.phase,
            },
        },
        "spec": {
            "backoffLimit": 0,  # Temporal owns retries, not the Job controller
            "activeDeadlineSeconds": JOB_ACTIVE_DEADLINE,
            "ttlSecondsAfterFinished": int(d.retention_seconds),
            "template": {
                "metadata": {
                    # Pod-level labels are the selector surface for operator
                    # policy (the chart's agent-job egress NetworkPolicy and
                    # any CNI policy engine) — keep project AND phase here,
                    # not just on the Job (issue #123).
                    "labels": {
                        "agents.homelab/project": d.project_id,
                        "agents.homelab/phase": spec.phase,
                    },
                },
                "spec": pod_spec,
            },
        },
    }


# --------------------------------------------------------------------------- #
# ConfigMap read/write helpers
# --------------------------------------------------------------------------- #
def _read_output(job_name: str) -> dict | None:
    """Return the parsed output ConfigMap payload, or None if not yet written."""
    data = cluster.read_configmap_data(job_name)
    if not data:
        return None
    raw = data.get(KEY_RESULT)
    if not raw:
        return None
    return json.loads(raw)


def _job_terminal(job) -> str | None:
    """Return 'complete', 'failed', or None for a batch/v1 Job object/dict."""
    status = job.status if not isinstance(job, dict) else job.get("status", {})
    succeeded = (
        getattr(status, "succeeded", None)
        if not isinstance(status, dict)
        else status.get("succeeded")
    )
    failed = (
        getattr(status, "failed", None)
        if not isinstance(status, dict)
        else status.get("failed")
    )
    if succeeded:
        return "complete"
    if failed:
        return "failed"
    return None


# --------------------------------------------------------------------------- #
# Polling
# --------------------------------------------------------------------------- #
async def _poll_to_terminal(
    batch, job_name: str, poll_interval: float
) -> AgentJobResult:
    """Poll a running Job until terminal or until it asks a human a question."""
    while True:
        payload = _read_output(job_name)
        if payload and payload.get("status") == JobStatus.AWAITING_HUMAN.value:
            log.info("job %s is awaiting a human reply", job_name)
            return AgentJobResult.from_payload(payload, job_name)

        job = batch.read_namespaced_job_status(job_name, NAMESPACE)
        terminal = _job_terminal(job)
        if terminal == "complete":
            payload = _read_output(job_name) or {"status": JobStatus.COMPLETE.value}
            return AgentJobResult.from_payload(payload, job_name)
        if terminal == "failed":
            payload = _read_output(job_name) or {}
            err = payload.get("error", f"Job {job_name} failed without output")
            raise ApplicationError(f"agent job failed: {err}", type="AgentJobFailed")

        await asyncio.sleep(poll_interval)


# --------------------------------------------------------------------------- #
# Activities
# --------------------------------------------------------------------------- #
@activity.defn
async def dispatch_agent_job(d: DispatchInput) -> AgentJobResult:
    """Render + create an Agent Execution Job, then poll it to completion.

    When ``JOB_RUNNER=docker``, delegates to the docker dispatch module which
    runs the agent as a ``docker run`` container (local quickstart path, issue
    #116). The K8s path remains the default.
    """
    if os.getenv("JOB_RUNNER") == "docker":
        from . import docker_dispatch

        log.info("dispatching agent job via docker (JOB_RUNNER=docker)")
        return await docker_dispatch.dispatch_agent_job_docker(d)

    attempt = activity.info().attempt
    # Jobs without an issue number (Alert Response diagnosis) share a name across
    # workflows; disambiguate by a hash of the workflow id so concurrent alerts
    # get distinct Jobs/ConfigMaps. Stable across retries (same workflow run).
    discriminator = ""
    if not d.issue_number:
        discriminator = hashlib.sha1(activity.info().workflow_id.encode()).hexdigest()[
            :8
        ]
    job_name = job_name_for(d, attempt, discriminator)
    manifest = render_job(d, job_name)

    batch = cluster.batch()

    from kubernetes.client.exceptions import ApiException

    try:
        batch.create_namespaced_job(NAMESPACE, manifest)
        log.info("created job %s (attempt %d)", job_name, attempt)
    except ApiException as exc:
        if getattr(exc, "status", None) != 409:  # already exists (retry attach)
            raise
        log.info("job %s already exists, attaching", job_name)

    return await _poll_to_terminal(batch, job_name, d.poll_interval_seconds)


@activity.defn
async def answer_agent_job(inp: AnswerInput) -> None:
    """Write a human's reply to the Job's input ConfigMap so it can resume."""
    cluster.patch_configmap_data(inp.job_name, {KEY_HUMAN_ANSWER: inp.answer})
    log.info("answered job %s", inp.job_name)


@activity.defn
async def await_agent_job(inp: AwaitInput) -> AgentJobResult:
    """Continue polling a Job that was previously parked on a human question."""
    return await _poll_to_terminal(
        cluster.batch(), inp.job_name, inp.poll_interval_seconds
    )


@activity.defn
async def cleanup_configmap(job_name: str) -> None:
    """Delete the output ConfigMap for a completed Agent Execution Job.

    The K8s Job itself is cleaned up natively via ttlSecondsAfterFinished.
    """
    from kubernetes.client.exceptions import ApiException

    try:
        cluster.core().delete_namespaced_config_map(job_name, NAMESPACE)
    except ApiException as exc:
        if getattr(exc, "status", None) != 404:
            log.warning("cleanup error for %s: %s", job_name, exc)
