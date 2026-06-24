"""Unit tests for Docker-based Agent Execution Job dispatch (issue #116).

Mocks the Docker client so no daemon is needed. Verifies the docker dispatch
seam: container creation with correct env / mounts, polling for the output
file, and result parsing into AgentJobResult.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from temporalio.testing import ActivityEnvironment

from devloop import docker_dispatch
from devloop.projects import ProjectConfig, _REGISTRY
from devloop.execution import DispatchInput, TaskSpec

_PROJECT = ProjectConfig(
    id="testproj",
    github_url="https://github.com/testorg/testproj",
    default_branch="main",
    agent_image="ghcr.io/omneval/devloop-agent-universal:latest",
    agent_label="agent-ready",
    omneval_ingest_secret="test-ingest",
    github_token_secret="test-github-token",
)


@pytest.fixture(autouse=True)
def _register_project():
    _REGISTRY.clear()
    _REGISTRY["testproj"] = _PROJECT
    yield
    _REGISTRY.clear()


def _dispatch_input(**kw):
    spec = TaskSpec(
        phase="execute",
        project_id="testproj",
        issue_number=42,
        title="Test feature",
        body="Do the thing",
    )
    return DispatchInput(
        project_id="testproj",
        issue_number=42,
        task_spec=spec,
        poll_interval_seconds=0.0,
        **kw,
    )


# --------------------------------------------------------------------------- #
# Tracer bullet: container runs and result is read from output file
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_docker_dispatch_runs_container_and_returns_result():
    """Docker dispatch should run the agent container with OUTPUT_FILE, wait
    for it to finish, and parse the AgentJobResult from the output file."""
    result_payload = {
        "status": "complete",
        "issue_number": 42,
        "branch": "devloop/testproj-execute-42",
        "commits": 2,
        "tests_passed": True,
    }

    container_mock = MagicMock()
    container_mock.wait.return_value = {"StatusCode": 0}

    captured_output_path = []

    def fake_run_container(image, env, output_path, bind_host_path, timeout=None):
        """Simulate container execution: write result to the bind-mounted
        host file (as the real container would write through the mount)."""
        captured_output_path.append(output_path)
        with open(bind_host_path, "w") as f:
            json.dump(result_payload, f)
        return 0

    with patch.object(
        docker_dispatch, "_run_container", side_effect=fake_run_container
    ):
        inp = _dispatch_input()
        result = await docker_dispatch.dispatch_agent_job_docker(inp)

    assert result.status == "complete"
    assert result.issue_number == 42
    assert result.commits == 2
    assert result.tests_passed is True
    # Verify the container was invoked
    assert len(captured_output_path) == 1


@pytest.mark.asyncio
async def test_docker_dispatch_raises_on_nonzero_exit():
    """Docker dispatch should raise ApplicationError when the container exits
    with a non-zero code and no output file exists."""

    def fail_run_container(*args, **kwargs):
        return 1

    with patch.object(
        docker_dispatch, "_run_container", side_effect=fail_run_container
    ):
        inp = _dispatch_input()
        with pytest.raises(Exception):  # ApplicationError
            await docker_dispatch.dispatch_agent_job_docker(inp)


# --------------------------------------------------------------------------- #
# Test _run_container calls docker SDK correctly
# --------------------------------------------------------------------------- #


def test_run_container_invokes_docker_sdk():
    """_run_container should call docker.from_env(), run a detached container
    with correct env/mounts, and wait for it."""
    container_mock = MagicMock()
    container_mock.wait.return_value = {"StatusCode": 0}

    output_file = "/tmp/test_output.json"
    with open(output_file, "w") as f:
        json.dump({"status": "complete", "issue_number": 1}, f)

    try:
        with patch("docker.from_env") as mock_from_env:
            client = MagicMock()
            mock_from_env.return_value = client
            client.containers.run.return_value = container_mock

            exit_code = docker_dispatch._run_container(
                image="test:latest",
                env={"KEY": "val", "OUTPUT_FILE": output_file},
                output_path=output_file,
                bind_host_path=output_file,
            )

            assert exit_code == 0
            client.containers.run.assert_called_once()
            call_args = client.containers.run.call_args
            # image is positional
            assert call_args.args[0] == "test:latest"
            assert call_args.kwargs["detach"] is True
            # No auto-remove: remove=True races container.wait() in docker-py
            # (the daemon can reap the container before wait() reads the exit
            # status). Removal happens explicitly after wait instead.
            assert "remove" not in call_args.kwargs
            container_mock.remove.assert_called_once_with(force=True)
            # OUTPUT_FILE should be in environment
            assert "OUTPUT_FILE" in call_args.kwargs["environment"]
            assert "KEY" in call_args.kwargs["environment"]
    finally:
        try:
            os.unlink(output_file)
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- #
# Test image resolution
# --------------------------------------------------------------------------- #


def test_resolve_image_uses_registry():
    """_resolve_image should use the registry's agent_image."""
    inp = _dispatch_input()
    image = docker_dispatch._resolve_image(inp)
    assert image == "ghcr.io/omneval/devloop-agent-universal:latest"


def test_resolve_image_uses_override():
    """image_override should win over registry agent_image."""
    inp = _dispatch_input(image_override="my-custom-image:v1")
    image = docker_dispatch._resolve_image(inp)
    assert image == "my-custom-image:v1"


# --------------------------------------------------------------------------- #
# Test env var forwarding
# --------------------------------------------------------------------------- #


def test_build_env_forwards_llm_vars():
    """_build_env should forward AGENT_MODEL and GITHUB_TOKEN from env."""
    env_overrides = {
        "AGENT_MODEL": "gpt-4o",
        "AGENT_LLM_BASE_URL": "https://example.com",
        "AGENT_LLM_API_KEY": "sk-test",
        "GITHUB_TOKEN": "ghp_test123",
        "AGENT_STUB": "test-stub",
    }
    with patch.dict(os.environ, env_overrides, clear=False):
        inp = _dispatch_input()
        env = docker_dispatch._build_env(inp)

    assert env["AGENT_MODEL"] == "gpt-4o"
    assert env["AGENT_LLM_BASE_URL"] == "https://example.com"
    assert env["AGENT_LLM_API_KEY"] == "sk-test"
    assert env["GITHUB_TOKEN"] == "ghp_test123"
    assert env["AGENT_STUB"] == "test-stub"
    assert env["PROJECT_ID"] == "testproj"
    assert "TASK_SPEC" in env


# --------------------------------------------------------------------------- #
# Test awaiting_human result passthrough
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_docker_dispatch_passes_awaiting_human():
    """Docker dispatch should return awaiting_human results without error."""
    result_payload = {
        "status": "awaiting_human",
        "issue_number": 42,
        "question": "Should I merge this?",
    }

    def fake_run(*args, **kwargs):
        with open(args[3], "w") as f:
            json.dump(result_payload, f)
        return 0

    with patch.object(docker_dispatch, "_run_container", side_effect=fake_run):
        inp = _dispatch_input()
        result = await docker_dispatch.dispatch_agent_job_docker(inp)

    assert result.status == "awaiting_human"
    assert result.question == "Should I merge this?"


# --------------------------------------------------------------------------- #
# Test JOB_RUNNER routing in dispatch_agent_job activity
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dispatch_agent_job_routes_to_docker_when_env_set():
    """dispatch_agent_job should delegate to docker_dispatch when
    JOB_RUNNER=docker."""
    result_payload = {
        "status": "complete",
        "issue_number": 42,
        "branch": "devloop/test",
        "commits": 1,
    }

    def fake_run(*args, **kwargs):
        with open(args[3], "w") as f:
            json.dump(result_payload, f)
        return 0

    with patch.dict(os.environ, {"JOB_RUNNER": "docker"}, clear=False):
        with patch.object(docker_dispatch, "_run_container", side_effect=fake_run):
            from devloop import k8s_jobs

            inp = _dispatch_input()
            result = await k8s_jobs.dispatch_agent_job(inp)

    assert result.status == "complete"


@pytest.mark.asyncio
async def test_dispatch_agent_job_uses_k8s_when_env_unset():
    """dispatch_agent_job should use the K8s path when JOB_RUNNER is not
    set to 'docker'."""
    from devloop import k8s_jobs
    from devloop.execution import AgentJobResult

    # Ensure JOB_RUNNER is not docker
    os.environ.pop("JOB_RUNNER", None)
    with patch.object(k8s_jobs, "render_job") as mock_render:
        with patch("devloop.k8s_jobs.cluster.batch"):
            with patch.object(k8s_jobs, "_poll_to_terminal") as mock_poll:
                mock_poll.return_value = AgentJobResult(
                    status="complete",
                    issue_number=42,
                    branch="test-branch",
                    job_name="test-job",
                )

                inp = _dispatch_input()
                await ActivityEnvironment().run(k8s_jobs.dispatch_agent_job, inp)

                # Should call render_job (K8s path)
                mock_render.assert_called_once()
