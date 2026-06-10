<!-- devloop-test: this file is the target of an automated end-to-end test run; safe to ignore/revert -->
# Getting Started with devloop

This guide walks you through the full path to running your first Dev Loop:
exposing a webhook ingress endpoint, setting up the `devloop-bot` GitHub
account, installing Temporal, deploying the devloop Helm chart, enrolling your
first project, and verifying everything works.

devloop is **fully autonomous and webhook-driven**. There is no poller, no
chat bot, and no human approval gates — once an issue is labeled
`agent-ready`, GitHub delivers a webhook event and the Dev Loop runs end to
end (Plan → Execute → CI Fix Loop → Review → Merge), posting status updates as
comments on the GitHub Issue and opening a PR for human consumption. The only
two devloop-specific images are `devloop-agent-base` (the shared toolchain
baked into every per-project agent image) and `devloop-temporal-worker` (the
single long-running deployment).

**Prerequisites**: A Kubernetes cluster with Helm 3 and `kubectl` configured,
and a public hostname or tunnel that can reach your cluster (see Step 1 — this
is a hard prerequisite, not optional).

## Step 1: Expose a Webhook Ingress Endpoint

Webhook ingress is **required** — devloop has no fallback polling mode. The
`devloop-temporal-worker` receives GitHub webhook events at
`/webhook/github`, and `https://<your-domain>/webhook/github` **must be
reachable by GitHub's servers** before you enroll any project. Choose one of
the following options:

**Option A — Cloudflare Tunnel (recommended; no inbound firewall ports needed):**

See [Cloudflare Tunnel setup](cloudflare-tunnel.md) for a full walkthrough of
creating a tunnel, routing DNS, and forwarding traffic to the in-cluster
`devloop-temporal-worker` Service. In short:

```bash
cloudflared tunnel login
cloudflared tunnel create devloop-webhooks
cloudflared tunnel route dns devloop-webhooks webhooks.your-domain.com
# Configure ingress to forward to devloop-temporal-worker.<namespace>.svc.cluster.local:8088
# and run cloudflared as a Deployment in the cluster (see the linked guide).
```

**Option B — Cloud load balancer (managed Kubernetes, e.g. EKS / GKE / AKS):**

The chart's `devloop-temporal-worker` Service is a fixed `ClusterIP` (it isn't
exposed externally by the chart). Create your own `LoadBalancer` Service in
the same namespace that selects the worker's pods and forwards to the webhook
port, then point a DNS record at the resulting external address/hostname:

```yaml
# devloop-temporal-worker-lb.yaml — apply alongside the chart release
apiVersion: v1
kind: Service
metadata:
  name: devloop-temporal-worker-lb
  namespace: agents
  annotations: {} # add your cloud provider's LB-class annotations here
spec:
  type: LoadBalancer
  selector:
    app.kubernetes.io/component: temporal-worker
    app.kubernetes.io/instance: devloop
  ports:
    - name: webhook
      port: 443
      targetPort: 8088
```

**Option C — ngrok (local testing / evaluation only):**

```bash
# Forward the temporal-worker webhook port to a public ngrok URL
ngrok http 8088
# Use the resulting https://xxxx.ngrok.io URL as your GitHub webhook Payload URL
```

Once you have a stable public hostname, confirm it can reach the
`devloop-temporal-worker` pod (a `405 Method Not Allowed` for a `GET` request
is the expected response from the POST-only `/webhook/github` endpoint):

```bash
curl -i https://<your-domain>/webhook/github
```

## Step 2: Create the GitHub Webhook

In each repository you plan to enroll, go to **Settings → Webhooks → Add
webhook** and configure:

- **Payload URL**: `https://<your-domain>/webhook/github` (the tunnel /
  load-balancer / ngrok URL from Step 1)
- **Content type**: `application/json`
- **Secret**: a strong random value — this becomes the `github-webhook-secret`
  Kubernetes Secret in Step 6
- **Events**: choose "Let me select individual events" and subscribe to:
  - **Issues** — the `labeled` action with the `agent-ready` label starts a
    new Dev Loop run
  - **Pull request reviews** — human review comments on an open agent PR
    (`agent/issue-<N>` branch) start a `PRCommentWorkflow` so the agent can
    respond
  - **Issue comments** — `@`-mentions of the `devloop-bot` account on an open
    agent PR (PRs are issues in the GitHub API) likewise start a
    `PRCommentWorkflow`

GitHub signs every delivery with `X-Hub-Signature-256` using the webhook
secret; devloop verifies this signature whenever `GITHUB_WEBHOOK_SECRET` is
set on the worker (strongly recommended — see Step 6).

## Step 3: Set Up GitHub Authentication for devloop-bot

devloop acts on GitHub as a dedicated bot identity (`devloop-bot` by
convention — configurable via `temporalWorker.agentGithubLogin`). This keeps
the agent's activity (PRs, review requests, status comments, replies) clearly
attributed and lets the webhook receiver filter out the bot's own
comments/reviews so they don't loop back and re-trigger workflows.

The standard way to do this is a **GitHub App** — registered and wired up in
3a–3d below. A real bot identity with no bot account to manage, short-lived
(1-hour) installation tokens minted on demand (nothing long-lived to leak or
rotate), per-repo installation scoping, and — critically for single-maintainer
setups — **working reviewer requests** (see the PAT fallback below for why a
PAT often can't deliver those).

devloop auto-detects which auth is configured: if `GITHUB_APP_ID` and
`GITHUB_APP_PRIVATE_KEY` are both set on the worker, it authenticates as a
GitHub App; otherwise it falls back to each project's PAT
(`github_token_secret`).

### 3a. Register the GitHub App

Go to **Settings → Developer settings → GitHub Apps → New GitHub App** (or
your organization's equivalent page), or paste this manifest into a
[GitHub App manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest)
to create it in one step:

```json
{
  "name": "devloop",
  "url": "https://github.com/<your-org>/devloop",
  "hook_attributes": { "url": "" },
  "redirect_url": "https://github.com/<your-org>/devloop",
  "public": false,
  "default_permissions": {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "checks": "read",
    "workflows": "write"
  },
  "default_events": []
}
```

If registering manually: name it `devloop` (or `devloop-<your-org>` — app
names are globally unique), keep the App's own **webhook disabled** (devloop's
webhook receiver from Step 2 is the integration point), and grant exactly
these **Repository permissions**: Contents (read/write), Pull requests
(read/write), Issues (read/write), Checks (read), Workflows (read/write) —
nothing else. **Don't skip Workflows**: without it, any agent branch touching
`.github/workflows/*` is rejected on push with an opaque "exit status 1".

### 3b. Generate a private key

On the app's settings page, scroll to **Private keys** → **Generate a private
key**. GitHub downloads a `.pem` file — the RSA key devloop uses to sign the
JWTs it exchanges for installation tokens. Note the **App ID** shown at the
top of the same page.

### 3c. Install the App on your repositories

From the app's page click **Install** and select the repos you're enrolling.
Note the **installation ID** — the numeric segment of the installation's
settings URL: `https://github.com/settings/installations/<installation_id>`.

### 3d. Store the private key as a Kubernetes Secret

```bash
kubectl create secret generic devloop-github-app-key \
  --from-file=privateKey=/path/to/devloop.<date>.private-key.pem \
  -n agents
```

You'll wire the App ID, installation ID, and this Secret into the chart in
Step 6d via the `githubApp.*` values:

```yaml
githubApp:
  appId: "123456"                      # GITHUB_APP_ID
  installationId: "987654"             # GITHUB_APP_INSTALLATION_ID
  privateKeySecret:
    name: devloop-github-app-key       # Secret from 3d
    key: privateKey
```

That's the App setup for the worker — every GitHub API call devloop's
workflows make (opening PRs, status comments, reviewer requests, labels) now
uses short-lived App installation tokens. One thing the App does **not**
cover: Agent Execution Job pods authenticate `git clone`/`git push` with the
per-project `github_token_secret` from the Project Registry (App installation
tokens expire after an hour, so they aren't mounted into jobs). You'll create
that Secret in Step 6b — it needs a token with the **Contents** and
**Workflows** read/write permissions (see the permission list in the PAT
fallback section below).

For the deeper reference — how token minting and refresh work, publishing the
app for other operators — see
**[GitHub App Setup for devloop-bot](github-app.md)**. Continue to
[Step 4](#step-4-install-temporal).

### Fallback — Fine-grained PAT (quick evaluation only)

If you'd rather not register a GitHub App for a quick single-repo evaluation,
a fine-grained PAT works, with one important caveat:

> **Reviewer requests don't work in the common single-maintainer setup.** A
> PAT authenticates as the account that created it; if that's also the human
> reviewer's account (typical for solo setups), GitHub forbids the formal
> review request — you can't request a review from yourself. devloop degrades
> to a best-effort workaround (assigning the PR and @-mentioning the reviewer
> in a comment instead), so `pr_reviewer` never produces a real "Review
> requested" entry. The GitHub App path doesn't have this problem: the App is
> its own identity, so review requests work even on a solo project.

1. **Create a GitHub account** dedicated to the bot (e.g. `devloop-bot`), and
   add it as a collaborator (or member, for an org) with write access to each
   repository you plan to enroll.
2. **Generate a fine-grained personal access token** (Settings → Developer
   settings → Personal access tokens → Fine-grained tokens) scoped to the
   enrolled repositories, with these **Repository permissions** (the same set
   the GitHub App path uses):
   - **Contents**: Read and write — clone, commit, and push branches
   - **Pull requests**: Read and write — open PRs, request reviewers, reply to
     review comments
   - **Issues**: Read and write — read labels, post status comments, open
     summarization digest issues
   - **Checks**: Read — poll CI status during the CI Fix Loop
   - **Workflows**: Read and write — push agent branches that add or edit
     `.github/workflows/*` files. **Don't skip this**: without it, a push
     touching workflow files is rejected with a generic-looking failure
     (`refusing to allow a Personal Access Token to create or update
     workflow ... without \`workflow\` scope`) — the job just fails with an
     opaque "exit status 1" and no obvious permission-related cause.
3. **Store the PAT as a Kubernetes Secret** — this is the value referenced by
   each project's `github_token_secret` in the Project Registry (Step 5a):

   ```bash
   kubectl create secret generic your-project-github-token \
     --from-literal=GITHUB_TOKEN=$DEVLOOP_BOT_PAT \
     -n agents
   ```

   (The key inside the Secret must be `GITHUB_TOKEN` — both the worker and
   the Agent Execution Job read that exact key.)

   Repeat per enrolled project (or reuse the same secret name across projects
   that share the same bot account and permission scope).

### Migrating an existing PAT deployment to the GitHub App

Existing PAT-based deployments keep working unchanged — App auth is opt-in.
To migrate: follow 3a–3d above, add the `githubApp.*` values to your Helm
release, and `helm upgrade`. The moment `GITHUB_APP_ID` and
`GITHUB_APP_PRIVATE_KEY` are both present on the worker, the worker's GitHub
API calls switch to App tokens and stop consulting each project's
`github_token_secret`. **Keep those Secrets**, though: Agent Execution Jobs
still mount them for `git clone`/`git push`. After migrating you can narrow
the PAT inside them to just **Contents** and **Workflows** read/write (the
worker no longer needs its Pull requests / Issues / Checks permissions).
There is no runtime fallback to the PAT if App-token minting fails (e.g. a
wrong `installationId` 404s on every call), so verify a Dev Loop run
end-to-end after upgrading.

## Step 4: Install Temporal

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

## Step 5: Agent Images (usually nothing to build)

**The default path requires no image build.** devloop publishes
`ghcr.io/omneval/devloop-agent-universal` — agent-base plus the common
language toolchains (Go, Node.js, Helm + helm-unittest). Any project whose
registry entry omits `agent_image` runs on it automatically (the chart pins
the version matching its own release; override via
`temporalWorker.agentJob.defaultImage`). Project-specific behaviour — phase
prompts, install/test commands, coding standards — lives in the enrolled
repo's own `.devloop/` directory, not in an image:

```
your-project/
  .devloop/
    config.yaml          # install: / tests: shell-command gates
    prompts/implement.md # optional per-phase prompt overrides
    skills/<name>/       # optional project-specific Agent Skills
      SKILL.md           # (full multi-file trees supported)
```

Build a custom image only when your project needs a toolchain the universal
image lacks:

### 5a (optional): Build a Per-Project Agent Image

Extend `devloop-agent-base` (the shared toolchain: OpenHands SDK, Temporal
SDK, `gh`, `kubectl`, `flux` — a build-time base layer, never a running pod)
or `devloop-agent-universal`. Write a `Dockerfile` in your project repository:

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

## Step 6: Deploy the devloop Chart

### 6a: Create the projects.yaml ConfigMap

The Project Registry tells devloop which repositories to monitor. Create a `projects.yaml` file:

**Minimal example** (required fields only — `agent_image` is optional and
defaults to the published `devloop-agent-universal`):

```yaml
projects:
  - id: your-project
    github_url: https://github.com/your-org/your-project
    default_branch: main
    agent_label: agent-ready
    omneval_ingest_secret: omneval-ingest-your-project
    github_token_secret: your-project-github-token
```

**Full example** (with the optional `agent_image` and `pr_reviewer`):

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

### 6b: Create Kubernetes Secrets

Create the secrets referenced in `projects.yaml` and by the worker itself.
(The GitHub App private-key Secret was already created in Step 3d.)

The per-project GitHub token Secret is needed in **both** auth modes: Agent
Execution Jobs mount it (key `GITHUB_TOKEN`) for `git clone`/`git push` — App
installation tokens are too short-lived for that. On the GitHub App path the
token only needs **Contents** and **Workflows** read/write; on the PAT
fallback path it's the full-permission PAT from Step 3.

```bash
# Per-project GitHub token for agent git operations (see Step 3)
kubectl create secret generic your-project-github-token \
  --from-literal=GITHUB_TOKEN=$GITHUB_TOKEN \
  -n agents

# Omneval ingest secret
kubectl create secret generic omneval-ingest-your-project \
  --from-literal=api-key=$OMNEVAL_INGEST_KEY \
  -n agents

# GitHub webhook secret — the same value you configured as the webhook
# "Secret" in Step 2. Used for HMAC-SHA256 signature verification on every
# inbound delivery.
kubectl create secret generic github-webhook-secret \
  --from-literal=secret=$GITHUB_WEBHOOK_SECRET \
  -n agents
```

Reference the `github-webhook-secret` from the worker via `extraEnv` in your
Helm values (see 6d) so the webhook receiver enforces signature verification:

```yaml
temporalWorker:
  extraEnv:
    - name: GITHUB_WEBHOOK_SECRET
      valueFrom:
        secretKeyRef:
          name: github-webhook-secret
          key: secret
```

### 6c: Create the ConfigMap

```bash
kubectl create configmap devloop-projects \
  --from-file=projects.yaml=./projects.yaml \
  -n agents
```

### 6d: Deploy with Helm

Create a `devloop-values.yaml`:

```yaml
temporalHost: temporal-frontend.agents.svc.cluster.local:7233

# GitHub App auth (Step 3) — omit this block only on the PAT fallback path
githubApp:
  appId: "123456"
  installationId: "987654"
  privateKeySecret:
    name: devloop-github-app-key
    key: privateKey

temporalWorker:
  agentGithubLogin: "devloop-bot"
  maxConcurrentJobs: 1
  ciFixMaxIterations: 5
  executeMaxIterations: 1
  maxQuestionsPerPhase: 3
  extraEnv:
    - name: GITHUB_WEBHOOK_SECRET
      valueFrom:
        secretKeyRef:
          name: github-webhook-secret
          key: secret

agent:
  gitName: "devloop-bot"
  gitEmail: "devloop-bot@users.noreply.github.com"

summarization:
  enabled: true
  cronSchedule: "0 8 * * 1"
  webhookUrl: ""
```

**How issue triggering works**: devloop is driven entirely by GitHub webhook
events — there is no polling loop and no interval to wait for. When you apply
the `agent-ready` label to a GitHub issue, GitHub sends an `issues` webhook
event (action `labeled`) to the public ingress endpoint you configured in Step
1. The `devloop-temporal-worker` receives the event at `/webhook/github`,
verifies its signature against `GITHUB_WEBHOOK_SECRET`, matches the repository
to an enrolled project, and starts a `DevLoopWorkflow` immediately. The same
endpoint also routes `pull_request_review` and `issue_comment` events from
human reviewers on open agent PRs into a `PRCommentWorkflow`, so the agent can
respond to feedback without any human needing to restart or approve anything.

**Fully autonomous — no approval gates**: Once triggered, the Dev Loop runs
Plan → Execute → CI Fix Loop → Review → Merge end to end without pausing for
human sign-off at any stage. If the agent has a clarifying question mid-run, a
fresh `Phase.ANSWER` agent is spawned to answer it autonomously (bounded by
`temporalWorker.maxQuestionsPerPhase`) rather than blocking on a human. The PR
that comes out the other end — plus the GitHub Issue comment trail — is the
review surface for humans; there is no separate plan-approval or merge-approval
step to configure.

**Workflow notifications**: All Dev Loop status updates (queued, implemented,
parked, review findings, CI Fix Loop progress) are posted as comments on the
relevant GitHub Issue using the project's `github_token_secret` (the
`devloop-bot` PAT from Step 3). Operators follow progress directly in GitHub —
no separate messaging platform is required.

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

## Step 7: Verify Dev Loop is Running

Check all deployments are healthy:

```bash
kubectl get pods -n agents
```

Expected pods — `devloop-temporal-worker` is the **only** long-running devloop
deployment (`devloop-agent-base` is a build-time base image, never a running
pod; per-project agent containers exist only transiently as Agent Execution
Jobs while a Dev Loop phase is active):

```
NAME                                        READY   STATUS    RESTARTS   AGE
devloop-temporal-worker-xxxxxxx             1/1     Running   0          2m
```

Check logs for the worker:

```bash
kubectl logs -n agents -l app.kubernetes.io/component=temporal-worker --tail=20
```

Create an issue in your GitHub repository with the `agent-ready` label. GitHub
delivers the `issues` webhook event immediately; the temporal-worker receives
it and starts the Dev Loop. Status comments appear on the GitHub Issue as the
Dev Loop progresses, and a PR opens automatically once the agent has changes
ready for review.

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

## Project Registry Schema

| Field                 | Required | Type  | Description                                      |
|-----------------------|----------|-------|--------------------------------------------------|
| `id`                  | Yes      | string | Unique project identifier                        |
| `github_url`          | Yes      | string | Full GitHub repository URL                       |
| `default_branch`      | Yes      | string | Default branch for PRs                           |
| `agent_image`         | No       | string | Container image for the project agent (defaults to the published `devloop-agent-universal` via `temporalWorker.agentJob.defaultImage`) |
| `agent_label`         | Yes      | string | GitHub issue label to trigger Dev Loop           |
| `omneval_ingest_secret` | Yes    | string | K8s secret name for Omneval ingest API key       |
| `github_token_secret` | Yes      | string | K8s secret name for the devloop-bot GitHub token (also used for posting issue comments). Used as the auth fallback when GitHub App auth (`githubApp.*` / `GITHUB_APP_ID` + `GITHUB_APP_PRIVATE_KEY`) is not configured — see [GitHub App Setup](github-app.md) |
| `pr_reviewer`         | No       | string | GitHub login requested as reviewer after CI Fix Loop and Review phases complete |

## Configuration Reference

| Setting                          | Description                                                                                   |
|----------------------------------|-----------------------------------------------------------------------------------------------|
| `temporalHost`                   | Temporal frontend gRPC address; set in Helm values to point at your Temporal cluster          |
| `GITHUB_TOKEN`                   | devloop-bot GitHub token used to post comments, open PRs, and request reviewers (per project via `github_token_secret`) — used when GitHub App auth is not configured |
| `GITHUB_APP_ID` / `githubApp.appId` | **Recommended** — GitHub App ID; together with `GITHUB_APP_PRIVATE_KEY` switches devloop-bot to GitHub App authentication (short-lived installation tokens) instead of a PAT. See [GitHub App Setup](github-app.md) |
| `GITHUB_APP_PRIVATE_KEY` / `githubApp.privateKeySecret` | RSA private key for the GitHub App, sourced from a K8s Secret (`{name, key}`) and forwarded via `secretKeyRef`. Used to sign the JWTs devloop exchanges for installation access tokens |
| `GITHUB_APP_INSTALLATION_ID` / `githubApp.installationId` | Selects which installation of the GitHub App devloop mints installation tokens for |
| `GITHUB_WEBHOOK_SECRET`          | HMAC secret for verifying GitHub webhook payload signatures (set on the temporal-worker pod via `extraEnv` + the `github-webhook-secret` Secret — strongly recommended) |
| `temporalWorker.agentGithubLogin`| GitHub login of the devloop-bot account (default `devloop-bot`). Forwarded as `AGENT_GITHUB_LOGIN`; the webhook receiver uses it to filter out the bot's own comments/reviews so they don't re-trigger workflows |
| `temporalWorker.maxConcurrentJobs` | Maximum number of Agent Execution Job dispatches (and LLM-bearing activities) that may run concurrently across all workflow types and projects. Forwarded as `MAX_CONCURRENT_JOBS`. Default `1` |
| `temporalWorker.ciFixMaxIterations` | Maximum number of `Phase.CI_FIX` retry attempts the CI Fix Loop spends trying to turn a PR's failing CI checks green before handing it to the human reviewer with a "CI still failing" note. Default `5` |
| `temporalWorker.executeMaxIterations` | Maximum number of Execute Agent Execution Job dispatch attempts the Execute phase retry loop spends when a dispatch produces zero commits, before parking the issue. Default `1` |
| `temporalWorker.maxQuestionsPerPhase` | Maximum number of mid-run `AWAITING_HUMAN` questions a single phase run may spawn `Phase.ANSWER` agent jobs for before the workflow proceeds with the agent's best guess. Default `3` |
| `agent.gitName`                  | Git author name used by Agent Execution Jobs when committing to enrolled repos (forwarded as `GIT_AUTHOR_NAME`). Default `homelab-agent` — set to your `devloop-bot` account name for clean attribution |
| `agent.gitEmail`                 | Git author email used by Agent Execution Jobs when committing (forwarded as `GIT_AUTHOR_EMAIL`). Default `agent@blosshomelab.com` — set to a `devloop-bot`-associated address |

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
