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
    - id: your-project
      github_url: https://github.com/your-org/your-project
      default_branch: main
      agent_image: ghcr.io/your-org/your-project-agent:latest
      agent_label: agent-ready
      discord_channel: agent-approvals
      omneval_ingest_secret: omneval-ingest-your-project
      github_token_secret: your-project-github-token
```

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
NAME                              READY   STATUS    RESTARTS   AGE
devloop-discord-bot-xxxxxxxxx     1/1     Running   0          2m
devloop-poller-xxxxxxxxx          1/1     Running   0          2m
devloop-temporal-worker-xxxxxxx  1/1     Running   0          2m
```

Check logs for each component:

```bash
kubectl logs -n agents -l app.kubernetes.io/component=temporal-worker --tail=20
kubectl logs -n agents -l app.kubernetes.io/component=poller --tail=20
kubectl logs -n agents -l app.kubernetes.io/component=discord-bot --tail=20
```

Create an issue in your GitHub repository with the `agent-ready` label. The poller should pick it up within 60 seconds, and the Discord bot should announce the Dev Loop in the configured channel.

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

| Setting               | Description                                              |
|-----------------------|----------------------------------------------------------|
| `temporalHost`        | Temporal frontend gRPC address; set in Helm values to point at your Temporal cluster |
| `GITHUB_TOKEN`        | GitHub PAT with `repo` and `workflow` scopes             |
| `DISCORD_TOKEN`       | Discord bot token for the approval channel               |
