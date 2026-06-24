"""Docker-based Agent Execution Job dispatch (issue #116).

Runs agent jobs as ``docker run`` containers instead of Kubernetes Jobs, using
the existing ``OUTPUT_FILE`` protocol in the agent entrypoint. Selectable via
``JOB_RUNNER=docker`` on the worker process.

The docker client is reached through ``docker.from_env()`` so unit tests can
monkeypatch the single seam without a Docker daemon.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

from temporalio.exceptions import ApplicationError

from .execution import AgentJobResult, DispatchInput
from .phases import JobStatus
from .projects import get_project

log = logging.getLogger(__name__)

# Path inside the container where the agent writes its result JSON. This must
# be a Linux-style path regardless of host OS, since the agent image is Linux.
_CONTAINER_OUTPUT_PATH = "/tmp/devloop-agent-output.json"

AGENT_BASE_IMAGE = os.getenv(
    "AGENT_BASE_IMAGE", "ghcr.io/omneval/devloop-agent-base:latest"
)
AGENT_DEFAULT_IMAGE = os.getenv(
    "AGENT_DEFAULT_IMAGE", "ghcr.io/omneval/devloop-agent-universal:latest"
)

# Docker-specific env vars forwarded from the worker process into the container.
_DOCKER_FWD_VARS = [
    "AGENT_MODEL",
    "AGENT_LLM_BASE_URL",
    "AGENT_LLM_API_KEY",
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
    "AGENT_SKILLS_BY_PHASE",
    "AGENT_SKILLS_SELECTION_MODE",
    "AGENT_SKILLS_CONFIGMAP",
]


def _resolve_image(d: DispatchInput) -> str:
    """Resolve the Docker image for a dispatch input.

    Uses the same resolution logic as the K8s path:
    image_override > registry agent_image > AGENT_DEFAULT_IMAGE / AGENT_BASE_IMAGE.
    """
    try:
        cfg = get_project(d.project_id)
    except KeyError:
        cfg = None

    image = d.image_override or (cfg.agent_image if cfg else "")
    if not image:
        image = AGENT_DEFAULT_IMAGE if cfg else AGENT_BASE_IMAGE
    return image


def _build_env(d: DispatchInput) -> dict[str, str]:
    """Build environment variables for the docker container."""
    spec = d.task_spec
    env: dict[str, str] = {
        "TASK_SPEC": spec.to_env_value(),
        "PROJECT_ID": d.project_id,
    }

    # Resolve GitHub URL and branch from registry or overrides
    try:
        cfg = get_project(d.project_id)
    except KeyError:
        cfg = None

    github_url = d.github_url_override or (cfg.github_url if cfg else "")
    if github_url:
        env["GITHUB_URL"] = github_url
    if cfg:
        env["DEFAULT_BRANCH"] = cfg.default_branch

    # GitHub token from local env (local quickstart uses the operator's gh auth)
    gh_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if gh_token:
        env["GITHUB_TOKEN"] = gh_token
        env["GH_TOKEN"] = gh_token

    # Forward LLM and agent config from the worker process
    for var in _DOCKER_FWD_VARS:
        val = os.environ.get(var)
        if val:
            env[var] = val

    # OUTPUT_FILE is set by the caller — the entrypoint writes to it
    return env


def _read_output(output_path: str) -> AgentJobResult:
    """Read and parse an AgentJobResult from the output file."""
    raw = Path(output_path).read_text()
    payload = json.loads(raw)
    if payload.get("status") == JobStatus.FAILED.value:
        raise ApplicationError(
            f"agent job failed: {payload.get('error', 'unknown error')}",
            type="AgentJobFailed",
        )
    return AgentJobResult.from_payload(payload, job_name="docker")


def _run_container(
    image: str,
    env: dict[str, str],
    output_path: str,
    bind_host_path: str,
    timeout: float | None = None,
) -> int:
    """Run a docker container and return the exit code.

    ``output_path`` is the path *inside* the container (must be a Linux-style
    path, e.g. ``/tmp/...``); ``bind_host_path`` is the corresponding file on
    the host that gets bind-mounted to it.
    """
    import docker

    client = docker.from_env()

    docker_env = {k: str(v) for k, v in env.items() if v is not None}
    # OUTPUT_FILE is the path *inside* the container (mounted at the same path)
    docker_env["OUTPUT_FILE"] = output_path

    container = None
    try:
        # No auto-remove: docker-py's remove=True races container.wait() (the
        # daemon can reap the container before wait() reads its exit status,
        # surfacing a 404 *after* the agent did all its work). We wait, grab
        # the exit code (and logs on failure), then remove explicitly.
        container = client.containers.run(
            image,
            command=["python", "/usr/local/bin/agent-entrypoint.py"],
            environment=docker_env,
            volumes={bind_host_path: {"bind": output_path, "mode": "rw"}},
            detach=True,
        )

        result = container.wait()
        exit_code = result.get("StatusCode", 1)
        if exit_code != 0:
            try:
                tail = container.logs(tail=50).decode(errors="replace")
                log.error(
                    "agent container exited %d; last log lines:\n%s",
                    exit_code,
                    tail,
                )
            except Exception:
                log.debug("could not read container logs", exc_info=True)
        return exit_code
    except Exception as exc:
        log.exception("docker container failed")
        raise ApplicationError(
            f"docker container error: {exc}",
            type="DockerContainerError",
        ) from exc
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                log.debug("could not remove container", exc_info=True)


async def dispatch_agent_job_docker(d: DispatchInput) -> AgentJobResult:
    """Run an Agent Execution Job via ``docker run``.

    Uses the existing ``OUTPUT_FILE`` protocol in the agent entrypoint: the
    container writes its result JSON to a bind-mounted file, which this function
    reads back and parses into an ``AgentJobResult``.

    This is the docker-based implementation of the dispatch seam, selectable via
    ``JOB_RUNNER=docker``. The K8s path in ``k8s_jobs`` remains the default.
    """
    image = _resolve_image(d)
    env = _build_env(d)

    # Create a temporary file for the output (host side); bind-mounted into
    # the container at _CONTAINER_OUTPUT_PATH, where the agent writes OUTPUT_FILE.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        output_path = tmp.name

    try:
        exit_code = await asyncio.to_thread(
            _run_container, image, env, _CONTAINER_OUTPUT_PATH, output_path
        )

        if exit_code != 0:
            # Try to read the output file for error details
            try:
                result = _read_output(output_path)
                if result.status != JobStatus.COMPLETE.value:
                    raise ApplicationError(
                        f"agent job failed (exit code {exit_code}): {result.error}",
                        type="AgentJobFailed",
                    )
                return result
            except (ApplicationError, FileNotFoundError, json.JSONDecodeError):
                raise ApplicationError(
                    f"agent job exited with code {exit_code}",
                    type="AgentJobFailed",
                )

        return _read_output(output_path)
    finally:
        # Clean up the temp file
        try:
            os.unlink(output_path)
        except FileNotFoundError:
            pass
