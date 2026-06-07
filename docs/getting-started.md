# Getting Started with devloop

This guide walks you through the full path to running your first Dev Loop: installing Temporal, deploying the devloop Helm chart, enrolling your first project, and verifying everything works.

**Prerequisites**: A Kubernetes cluster with Helm 3 and `kubectl` configured.

## Step 1: Expose a Webhook Ingress Endpoint

Webhook ingress is required. The `devloop-temporal-worker` receives GitHub webhook events at `/webhook/github` and must be reachable from GitHub's servers. Choose one of the following options before proceeding:

**Option A — Cloudflare Tunnel (recommended for production):**
```bash
# Install cloudflared and create a tunnel that forwards to the temporal-worker service
cloudflared tunnel create devloop
cloudflared tunnel route dns devloop webhooks.your-domain.com
# Add the tunnel credentials as a Kubernetes secret, then deploy the cloudflared pod
```

**Option B — Cloud load balancer (managed Kubernetes, e.g. EKS / GKE / AKS):**
```yaml
# Add to devloop-values.yaml — exposes the temporal-worker webhook port via a
# cloud-provisioned LoadBalancer. Use annotations for your cloud provider's LB class.
temporalWorker:
  service:
    type: LoadBalancer
    webhookPort: 8088
```

**Option C — ngrok (local testing only):**
```bash
# Forward the temporal-worker webhook port to a public ngrok URL
ngrok http 8088
# Use the https://xxxx.ngrok.io URL as your GitHub webhook URL
```

Once the endpoint is reachable, configure a GitHub webhook in each enrolled repository:

- **Payload URL**: `https://<your-public-host>/webhook/github`
- **Content type**: `application/json`
- **Secret**: the value you will set as `GITHUB_WEBHOOK_SECRET` (optional but recommended)
- **Events**: select "Issues" (the `labeled` action triggers Dev Loops)

## Step 2: Install Temporal

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

## Step 3: Build the Agent Base Image

The agent base image provides the shared toolchain (OpenHands SDK, Temporal SDK, `gh`, `kubectl`, `flux`). Build and push it to your registry:

```bash
docker build -t ghcr.io/your-org/devloop-agent-base:latest images/agent-base/
docker push ghcr.io/your-org/devloop-agent-base:latest
```

## Step 4: Build a Per-Project Agent Image

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

## Step 5: Deploy the devloop Chart

### 5a: Create the projects.yaml ConfigMap

The Project Registry tells devloop which repositories to monitor. Create a `projects.yaml` file:

**Minimal example** (required fields only):

```yaml
projects:
  - id: your-project
    github_url: https://github.com/your-org/your-project
    default_branch: main
    agent_image: ghcr.io/your-org/your-project-agent:latest
    agent_label: agent-ready
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
    omneval_ingest_secret: omneval-ingest-your-project
    github_token_secret: your-project-github-token
    pr_reviewer: "your-github-reviewer-username"
```

### 5b: Create Kubernetes Secrets

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

### 5c: Create the ConfigMap

```bash
kubectl create configmap devloop-projects \
  --from-file=projects.yaml=./projects.yaml \
  -n agents
```

### 5d: Deploy with Helm

Create a `devloop-values.yaml`:

```yaml
temporalHost: temporal-frontend.agents.svc.cluster.local:7233
```

**How issue triggering works**: devloop uses GitHub webhook events. When you apply the `agent-ready` label to a GitHub issue, GitHub sends an `issues` webhook event to the public ingress endpoint you configured in Step 1. The `devloop-temporal-worker` receives the event at `/webhook/github` and starts a Dev Loop workflow. Webhook delivery is instant — no polling interval to wait for.

**Workflow notifications**: All Dev Loop status updates (queued, implemented, parked, review findings) are posted as comments on the relevant GitHub Issue using the project's `github_token_secret`. Operators can follow progress directly in GitHub without a separate messaging platform.

**Weekly summaries**: Once a week (Monday 08:00 UTC by default), devloop opens a GitHub Issue on each enrolled repo titled `[devloop] <project-id> — <date> digest`, labeled `devloop-summary` (the label is created automatically if it does not exist), summarizing the week's merged changes and closed issues in plain English. No extra configuration is required — see `summarization.*` below to customize the schedule, disable it, or forward the digest to an outbound webhook.

Deploy:

```bash
helm repo add devloop https://charts.omneval.dev/devloop
helm repo update
helm install devloop devloop/devloop \
  --namespace agents \
  --create-namespace \
  -f devloop-values.yaml
```

## Step 6: Verify Dev Loop is Running

Check all deployments are healthy:

```bash
kubectl get pods -n agents
```

Expected pods:

```
NAME                                        READY   STATUS    RESTARTS   AGE
devloop-temporal-worker-xxxxxxx             1/1     Running   0          2m
```

Check logs for each component:

```bash
kubectl logs -n agents -l app.kubernetes.io/component=temporal-worker --tail=20
```

Create an issue in your GitHub repository with the `agent-ready` label. GitHub delivers the webhook event immediately; the temporal-worker will receive it and start the Dev Loop. Status comments will appear on the GitHub Issue as the Dev Loop progresses.

## Manually Triggering or Restarting a Dev Loop

If a workflow finishes while open `agent-ready` issues remain in the repository, those issues will not be re-triggered automatically. Use one of these approaches:

**Open a new issue** — create a fresh issue with the `agent-ready` label. GitHub delivers the webhook event immediately, starting a new Dev Loop run.

**Re-send the webhook** — use `scripts/restart_workflows.py` to post a trigger event directly to the temporal-worker webhook endpoint (see the Troubleshooting section in the README).

**Use the Temporal CLI** — start a workflow directly:

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
reuse SDK activities for Kubernetes Job dispatch and GitHub Issue notifications.

## Project Registry Schema

| Field                 | Required | Type  | Description                                      |
|-----------------------|----------|-------|--------------------------------------------------|
| `id`                  | Yes      | string | Unique project identifier                        |
| `github_url`          | Yes      | string | Full GitHub repository URL                       |
| `default_branch`      | Yes      | string | Default branch for PRs                           |
| `agent_image`         | Yes      | string | Container image for the project agent             |
| `agent_label`         | Yes      | string | GitHub issue label to trigger Dev Loop           |
| `omneval_ingest_secret` | Yes    | string | K8s secret name for Omneval ingest API key       |
| `github_token_secret` | Yes      | string | K8s secret name for GitHub agent token (also used for posting issue comments) |
| `pr_reviewer`         | No       | string | Optional GitHub login tagged for review on merge PRs |

## Configuration Reference

| Setting                          | Description                                                                                   |
|----------------------------------|-----------------------------------------------------------------------------------------------|
| `temporalHost`                   | Temporal frontend gRPC address; set in Helm values to point at your Temporal cluster          |
| `GITHUB_TOKEN`                   | GitHub token used by devloop-bot to post comments on GitHub Issues (per project via `github_token_secret`) |
| `GITHUB_WEBHOOK_SECRET`          | Optional HMAC secret for verifying GitHub webhook payloads (set on the temporal-worker pod)   |

### Summarization (`summarization.*`)

Controls the weekly Summarization workflow and its Temporal Schedule (one schedule per enrolled project, `summarize-weekly-<project-id>`). Delivery defaults to opening a GitHub Issue — no extra configuration required.

| Helm value                  | Default          | Description                                                                                                  |
|-----------------------------|------------------|--------------------------------------------------------------------------------------------------------------|
| `summarization.enabled`     | `true`           | When `false`, devloop does not create the weekly summarization schedule for any project (and deletes any existing one on the next worker startup). |
| `summarization.cronSchedule`| `"0 8 * * 1"`    | 5-field cron expression (`minute hour day-of-month month day-of-week`) controlling when the weekly digest runs. Default is Monday 08:00. Forwarded to the Temporal `ScheduleCalendarSpec`; only plain integers and `*` are supported per field — anything richer falls back to the default Monday 08:00 schedule. |
| `summarization.webhookUrl`  | `""`             | Optional outbound webhook URL. When set, devloop POSTs `{"project_id": ..., "summary": ..., "date": ...}` as JSON to this URL in addition to opening the GitHub Issue. Forwarded to the worker as `SUMMARIZATION_WEBHOOK_URL`. Delivery is fire-and-forget — failures are logged but never fail the workflow. |

Example:

```yaml
summarization:
  enabled: true
  cronSchedule: "0 9 * * 1"   # Monday 09:00 instead of the 08:00 default
  webhookUrl: "https://hooks.example.com/devloop-digest"
```

**Delivery**: Each run opens a GitHub Issue titled `[devloop] <project-id> — <date> digest` on the enrolled repo, with the digest as the issue body and the label `devloop-summary` (created automatically on first use). The issue is opened by devloop-bot using the project's `github_token_secret`.
