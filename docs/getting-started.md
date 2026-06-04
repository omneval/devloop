# Getting Started with devloop

This guide walks you through the full path to running your first Dev Loop: installing Temporal, deploying the devloop Helm chart, enrolling your first project, and verifying everything works.

**Prerequisites**: A Kubernetes cluster with Helm 3 and `kubectl` configured.

## Step 1: Install Temporal

devloop requires a Temporal cluster. See [Temporal Prerequisites](temporal-prerequisites.md) for a complete reference. Quick start:

```bash
helm repo add temporal https://charts.temporalio.io
helm repo update
helm install temporal temporal/temporal \
  --namespace agents \
  --create-namespace \
  -f docs/reference-temporal-values.yaml
```

Verify the Temporal frontend is running:

```bash
kubectl get pods -n agents -l app=temporal
```

Note the service address for later:

```
temporal-frontend.agents.svc.cluster.local:7233
```

## Step 2: Build the Agent Base Image

The agent base image provides the shared toolchain (OpenHands SDK, Temporal SDK, `gh`, `kubectl`, `flux`). Build and push it to your registry:

```bash
docker build -t ghcr.io/your-org/devloop-agent-base:latest images/agent-base/
docker push ghcr.io/your-org/devloop-agent-base:latest
```

## Step 3: Build a Per-Project Agent Image

Each project gets its own agent image that extends `devloop-agent-base`. Write a `Dockerfile` in your project repository:

```dockerfile
FROM ghcr.io/your-org/devloop-agent-base:latest

# Install project-specific tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Install project-specific Python packages
RUN uv pip install --system --no-cache \
    "requests>=2.31"

# Add project-specific scripts or configuration
COPY scripts/ /usr/local/share/agent-scripts/
```

Build and push:

```bash
docker build -t ghcr.io/your-org/your-project-agent:latest .
docker push ghcr.io/your-org/your-project-agent:latest
```

Tag images with Git SHAs for reproducibility:

```bash
docker tag ghcr.io/your-org/your-project-agent:latest \
  ghcr.io/your-org/your-project-agent:sha-$(git rev-parse --short HEAD)
```

## Step 4: Deploy the devloop Chart

### 4a: Create the projects.yaml ConfigMap

The Project Registry tells devloop which repositories to monitor. Create a `projects.yaml` file:

**Minimal example** (required fields only):

```yaml
projects:
  - id: your-project
    github_url: https://github.com/your-org/your-project
    default_branch: main
    agent_image: ghcr.io/your-org/your-project-agent:latest
    agent_label: agent-ready
    discord_channel: agent-approvals
    omneval_ingest_secret: omneval-ingest-your-project
    github_token_secret: your-project-github-token
```

**Full example** (with optional `pr_reviewer`):

```yaml
projects:
  - id: your-project
    github_url: https://github.com/your-org/your-project
    default_branch: main
    agent_image: ghcr.io/your-org/your-project-agent:latest
    agent_label: agent-ready
    discord_channel: agent-approvals
    omneval_ingest_secret: omneval-ingest-your-project
    github_token_secret: your-project-github-token
    pr_reviewer: "https://api.openai.com/v1"
```

### 4b: Create Kubernetes Secrets

Create the secrets referenced in `projects.yaml`:

```bash
# GitHub token for the agent
kubectl create secret generic your-project-github-token \
  --from-literal=token=$GITHUB_TOKEN \
  -n agents

# Omneval ingest secret
kubectl create secret generic omneval-ingest-your-project \
  --from-literal=api-key=$OMNEVAL_INGEST_KEY \
  -n agents
```

### 4c: Create the ConfigMap

```bash
kubectl create configmap devloop-projects \
  --from-file=projects.yaml=./projects.yaml \
  -n agents
```

### 4d: Deploy with Helm

Create a `devloop-values.yaml`:

```yaml
temporalHost: temporal-frontend.agents.svc.cluster.local:7233

discordBot:
  enabled: true
  token: "your-discord-bot-token"

poller:
  githubToken: "ghp_your-github-personal-access-token"
  projects:
    - repo: "your-org/your-project"
      label: "agent-ready"
      webhookUrl: "http://devloop-temporal-worker.agents.svc.cluster.local:8088/webhook/github"
```

**How issue triggering works**: devloop uses a polling model rather than a direct GitHub webhook. The `devloop-poller` Deployment periodically queries the GitHub Issues API for issues carrying `label`, then forwards any *new* ones to the Temporal Orchestration Worker's internal webhook endpoint (`webhookUrl`). No public-facing URL or GitHub webhook configuration is required. The poller persists seen issue numbers across restarts so the same issue never triggers twice. One poller Deployment is created per entry in `poller.projects`.

Deploy:

```bash
helm repo add devloop https://charts.omneval.dev/devloop
helm repo update
helm install devloop devloop/devloop \
  --namespace agents \
  --create-namespace \
  -f devloop-values.yaml
```

## Step 5: Verify Dev Loop is Running

Check all deployments are healthy:

```bash
kubectl get pods -n agents
```

Expected pods:

```
NAME                                        READY   STATUS    RESTARTS   AGE
devloop-discord-bot-xxxxxxxxx               1/1     Running   0          2m
devloop-poller-yourorgourproject-xxxxxxxxx  1/1     Running   0          2m
devloop-temporal-worker-xxxxxxx             1/1     Running   0          2m
```

One poller pod is created per entry in `poller.projects`; the pod name is derived from the repository name.

Check logs for each component:

```bash
kubectl logs -n agents -l app.kubernetes.io/component=temporal-worker --tail=20
kubectl logs -n agents -l app.kubernetes.io/component=poller --tail=20
kubectl logs -n agents -l app.kubernetes.io/component=discord-bot --tail=20
```

Create an issue in your GitHub repository with the `agent-ready` label. The poller checks GitHub every `pollIntervalSeconds` (default: 300 s / 5 min) and will forward it on the next cycle. The Discord bot should then announce the Dev Loop in the configured channel.

> **Note:** The poller tracks seen issue numbers, so the same issue will not trigger a second run automatically. To restart a failed workflow, open a new issue with the label or start a workflow directly via the Temporal CLI (see below).

## Manually Triggering or Restarting a Dev Loop

The poller only forwards *new* issue numbers it has not seen before, so re-labeling an existing issue will not restart a failed or completed workflow. Use one of these approaches instead:

**Open a new issue** — create a fresh issue with the `agent-ready` label. It has a new number, so the poller will forward it on the next cycle.

**Use the Temporal CLI** — start a workflow directly without going through the poller:

```bash
temporal workflow start \
  --workflow-type DevLoopWorkflow \
  --task-queue homelab-orchestration \
  --workflow-id devloop-<project-id> \
  --input '{"project_id": "<project-id>", "agent_label": "agent-ready"}'
```

Replace `<project-id>` with the value from your Project Registry. Because the old run is in a terminal state (Failed or Completed), starting with the same workflow ID creates a clean new execution.

## Extending devloop with Custom Workflows

The devloop temporal worker can run custom workflows alongside the built-in
DevLoop and Summarization workflows.  See the
[Alert Response Workflow example](examples/alert-response/README.md) for a
complete consumer extension pattern: install `omneval-devloop` as a dependency,
write a custom `@workflow.defn`, register both in a single worker process, and
reuse SDK activities for Kubernetes Job dispatch and Discord messaging.

## Project Registry Schema

| Field                 | Required | Type  | Description                                      |
|-----------------------|----------|-------|--------------------------------------------------|
| `id`                  | Yes      | string | Unique project identifier                        |
| `github_url`          | Yes      | string | Full GitHub repository URL                       |
| `default_branch`      | Yes      | string | Default branch for PRs                           |
| `agent_image`         | Yes      | string | Container image for the project agent             |
| `agent_label`         | Yes      | string | GitHub issue label to trigger Dev Loop           |
| `discord_channel`     | Yes      | string | Discord channel name for Dev Loop approvals      |
| `omneval_ingest_secret` | Yes    | string | K8s secret name for Omneval ingest API key       |
| `github_token_secret` | Yes      | string | K8s secret name for GitHub agent token           |
| `pr_reviewer`         | No       | string | Optional API endpoint for PR review automation   |

## Configuration Reference

| Setting                          | Description                                                                                   |
|----------------------------------|-----------------------------------------------------------------------------------------------|
| `temporalHost`                   | Temporal frontend gRPC address; set in Helm values to point at your Temporal cluster          |
| `DISCORD_TOKEN`                  | Discord bot token for the approval channel                                                    |
| `poller.githubToken`             | GitHub PAT with `repo` read scope used by all poller instances                                |
| `poller.githubTokenSecret`       | Kubernetes Secret reference for the GitHub token (preferred over plain `githubToken`)         |
| `poller.pollIntervalSeconds`     | Seconds between GitHub API poll cycles (default: `300`)                                       |
| `poller.projects[].repo`         | Full GitHub repository name, e.g. `your-org/your-project`                                    |
| `poller.projects[].label`        | Issue label that triggers the Dev Loop (default: `agent-ready`)                               |
| `poller.projects[].webhookUrl`   | Internal URL of the Temporal Orchestration Worker webhook endpoint                            |
