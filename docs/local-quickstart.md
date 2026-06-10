<!-- devloop-test: this file is the target of an automated end-to-end test run; safe to ignore/revert -->
# Local Quickstart — Dev Loop without a Cluster

This guide walks through running the full Dev Loop on a single machine with
zero Kubernetes, no public hostname, and no self-deployed Temporal cluster.
The only prerequisites are **Docker**, **`gh`**, and a **GitHub account**
with repository access.

End result: labeling an issue `agent-ready` on a real GitHub repo produces a
real draft PR — all from localhost.

## Prerequisites

| Tool | Install |
|------|---------|
| **Docker + Docker Compose** | https://docs.docker.com/get-docker/ |
| **GitHub CLI** | `brew install gh` / `sudo apt install gh` — then `gh auth login` |
| **Python + uv** | `curl -LsSf https://astral.sh/uv/install.sh | sh` |
| **devloop source** | `git clone https://github.com/omneval/devloop.git && cd devloop` |

## Step 1 — Start Local Infrastructure

The included [`docker-compose.yml`](../docker-compose.yml) starts a Temporal
server (with embedded Cassandra) and the Temporal Web UI in one command:

```bash
docker compose up -d
```

This launches:

| Service | Port | Purpose |
|---------|------|---------|
| `temporal` | `7233` | Temporal gRPC server (default namespace) |
| `temporal-web` | `8233` | Temporal Web UI |
| `cassandra` | (internal) | Persistence layer for Temporal |

> **Alternative**: If you prefer the Temporal CLI, you can skip the compose
> file and run `temporal server start-dev` instead.

Confirm Temporal is reachable:

```bash
docker compose ps
# All three services should be "running"
```

## Step 2 — Forward GitHub Webhooks Locally

`gh webhook forward` bridges real GitHub webhook deliveries to localhost
without any tunnel or public hostname:

```bash
gh webhook forward \
  --repo=OWNER/REPO \
  --events=issues,issue_comment,pull_request_review \
  --url=http://localhost:8088/webhook/github
```

Replace `OWNER/REPO` with your target repository. Keep this running in a
separate terminal — it stays connected to GitHub until you stop it with
`Ctrl+C`.

**Why this works**: GitHub sends webhook events directly to the `gh` CLI's
local tunnel, which relays them to `http://localhost:8088/webhook/github`.
No DNS, no TLS, no firewall changes.

## Step 3 — Configure the Worker

Create a minimal project registry. The worker reads this file at startup to
know which repos to monitor:

```bash
mkdir -p agents
cat > agents/projects.yaml <<'EOF'
projects:
  - id: my-project
    github_url: https://github.com/OWNER/REPO
    default_branch: main
    agent_label: agent-ready
    omneval_ingest_secret: ""
    github_token_secret: ""
EOF
```

> **Note**: The `omneval_ingest_secret` and `github_token_secret` fields are
> required by the schema but can be empty strings for local evaluation. The
> worker authenticates to GitHub via the `gh` CLI's stored credential when
> `GITHUB_TOKEN` is not set as a separate secret.

## Step 4 — Run the Worker Locally

Start the Temporal worker in the devloop source directory. Set `JOB_RUNNER=docker`
so that agent jobs execute via `docker run` instead of Kubernetes Jobs:

```bash
export JOB_RUNNER=docker
export TEMPORAL_ADDRESS=localhost:7233
export TEMPORAL_NAMESPACE=default
export GITHUB_TOKEN=$(gh auth token)
export AGENT_MODEL=gpt-4o
export AGENT_LLM_API_KEY="your-api-key-here"

uv sync --all-groups
uv run python -m devloop.worker \
  --project-registry agents/projects.yaml
```

The worker starts the temporal worker process, listens on `:8088` for webhook
events, and registers activities including `dispatch_agent_job` (which now
delegates to Docker when `JOB_RUNNER=docker`).

## Step 5 — Trigger the Dev Loop

1. Create an issue in your `OWNER/REPO` repository (via GitHub UI or `gh issue create`).
2. Label it `agent-ready`:

```bash
gh issue edit <NUMBER> --add-label agent-ready
```

GitHub delivers the `issues` webhook event → `gh webhook forward` relays it to
the worker → the Dev Loop workflow starts on Temporal.

## What Happens Next

The Dev Loop runs end to end:

1. **Plan** — the worker runs a lightweight `plan_issue` activity that analyzes
   the issue and produces a task specification.
2. **Execute** — `dispatch_agent_job` runs the agent image via `docker run`,
   mounting a local output file for results (the `OUTPUT_FILE` protocol).
3. **CI Fix Loop** — if CI checks fail, the agent retries up to
   `CI_FIX_MAX_ITERATIONS` times.
4. **Review & Merge** — a draft PR opens on the enrolled repo.

You can watch Temporal's UI at http://localhost:8233 to see the workflow
progress in real time.

## Docker Dispatch Mode

When `JOB_RUNNER=docker`, the `dispatch_agent_job` activity delegates to
`devloop.docker_dispatch` instead of creating a Kubernetes Job. The Docker
path:

1. Resolves the agent image (from the project registry, `image_override`, or
   `AGENT_DEFAULT_IMAGE` falling back to `devloop-agent-universal`).
2. Creates a temporary output file on disk.
3. Runs `docker run` with:
   - The agent image as the container
   - `OUTPUT_FILE` pointing to the temp file (bind-mounted via `volumes`)
   - All relevant environment variables (`PROJECT_ID`, `TASK_SPEC`, LLM config,
     GitHub token, etc.)
4. Waits for the container to exit.
5. Reads the JSON result from the output file and returns an `AgentJobResult`.

This bridges the existing `OUTPUT_FILE` protocol — originally designed for
Kubernetes ConfigMaps — to a local Docker run, without forking the workflow
code.

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `JOB_RUNNER` | Set to `docker` to use Docker dispatch instead of Kubernetes | (K8s) |
| `TEMPORAL_ADDRESS` | Temporal server gRPC address | `localhost:7233` |
| `TEMPORAL_NAMESPACE` | Temporal namespace | `default` |
| `GITHUB_TOKEN` | GitHub token for agent auth | (from `gh auth token`) |
| `AGENT_MODEL` | LLM model for the agent | (from chart default) |
| `AGENT_LLM_API_KEY` | LLM API key | (required) |
| `AGENT_LLM_BASE_URL` | LLM base URL (optional, for local models) | (provider default) |
| `AGENT_STUB` | If set, the agent returns a stub response instead of calling the LLM | (disabled) |

## Troubleshooting

**Worker fails to connect to Temporal**: Make sure the compose services are
running (`docker compose ps`) and `TEMPORAL_ADDRESS` is set correctly.

**Webhook events not arriving**: Check that `gh webhook forward` is connected
(`gh webhook forward` shows delivery logs). Verify the repo webhook
configuration matches the forwarded events.

**Docker runs fail with "image not found"**: The worker resolves the agent
image from the project registry's `agent_image` field, falling back to
`AGENT_DEFAULT_IMAGE` then to `ghcr.io/omneval/devloop-agent-universal:latest`.
If you do not have a custom image, ensure you can pull the universal image or
set `AGENT_DEFAULT_IMAGE` to a locally available image.

**Agent job returns empty result**: Check the Docker container logs:

```bash
docker ps -a --filter "status=exited" --format "{{.ID}} {{.Image}} {{.Status}}"
docker logs <container-id>
```

## Cleanup

```bash
docker compose down       # stop and remove Temporal + Cassandra + Web UI
# Stop the gh webhook forward (Ctrl+C in its terminal)
# Stop the worker (Ctrl+C in its terminal)
```

No persistent state remains — the compose stack stores data in memory only,
and Docker containers are removed after each run (`--rm`).
