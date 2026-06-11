# devloop

[![CI](https://github.com/omneval/devloop/actions/workflows/ci.yml/badge.svg)](https://github.com/omneval/devloop/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://github.com/omneval/devloop/blob/main/LICENSE)
[![PyPI](https://img.shields.io/pypi/v/omneval-devloop.svg)](https://pypi.org/project/omneval-devloop/)
[![Python >=3.12](https://img.shields.io/badge/python->=3.12-blue.svg)](https://www.python.org/downloads/)

devloop is an open-source framework that runs autonomous, agent-driven code improvement workflows on your own Kubernetes cluster. It packages the Dev Loop engine — Plan → Execute → CI Fix Loop → Review — as a reusable Python SDK (`omneval-devloop`), two container images, and a Helm chart so any team can deploy it without forking.

Triggered by GitHub webhook events, devloop processes issues labeled `agent-ready`: an OpenHands agent reads the issue, writes code, opens a PR, and requests a reviewer. There are no human-approval gates and devloop never merges — it posts a summary comment for a human to review and merge.

## Key Features

- **Autonomous Dev Loop** — Multi-phase workflow (Plan → Execute → CI Fix Loop → Review) that improves enrolled codebases end to end with no human approval gates.
- **Reusable Python SDK** — The `omneval-devloop` package ships the Dev Loop workflows, activities, and shared dataclasses as a testable library consumers import alongside their own Temporal workflows.
- **Webhook-driven** — GitHub delivers `issues`, `pull_request_review`, and `issue_comment` events directly to the temporal-worker; no poller, no chat bot.
- **CI Fix Loop** — After the agent pushes changes, failing CI checks trigger automatic fix attempts (up to a configurable limit) before the PR is handed to a human reviewer.
- **PR Comment Re-engagement** — Human review comments or `@devloop-bot` mentions on an open agent PR re-engage the agent on the existing branch.
- **Summarization** — A scheduled workflow generates plain-English digests of closed issues and git diffs, posted as GitHub Issues and optionally forwarded to an outbound webhook.
- **Repo-native config** — An enrolled repo can carry `.devloop/config.yaml` (install/test commands that gate its PRs), `.devloop/prompts/<phase>.md` (per-phase prompt overrides), and `.devloop/skills/<name>/` (project-specific Agent Skills, multi-file trees included) so agent customization versions with the code — no per-project image rebuild needed.
- **Agent Skills** — Reusable, model-agnostic capabilities in the AgentSkills format, with progressive disclosure and per-phase allowlists via `skillsByPhase`. The batteries-included skills are taken from https://github.com/mattpocock/skills . Go check out his stuff!
- **Eval flywheel** — Every phase emits KPI span attributes (commits, per-suite test results, criteria-audit passes, review verdicts, loop iterations, label→PR wall-clock) into omneval, and `devloop-bench` replays a golden set of closed issues scored by an LLM judge so prompt/model/harness changes become measured A/B decisions. See the "KPI span attributes" section in [CONTEXT.md](CONTEXT.md).

## Prerequisites

- **Python >= 3.12** — the SDK and helper scripts require Python 3.12 or later.
- **[uv](https://github.com/astral-sh/uv)** — Python package manager used exclusively for dependency management.
- **Docker** — only needed if you build a custom agent image; the published `devloop-agent-universal` image (Go, Node.js, Helm toolchains) covers most projects out of the box.
- **Kubernetes cluster** — devloop deploys as a Helm chart; `kubectl` must be configured.
- **Helm 3** — for deploying the `charts/devloop/` chart.
- **Temporal** — durable orchestration layer. Either deploy it independently (see [Temporal Prerequisites](docs/temporal-prerequisites.md)) or let the chart bundle it for evaluation with `--set temporal.enabled=true`.
- **Public webhook endpoint** — a hostname or tunnel (Cloudflare Tunnel, ngrok, load balancer) that GitHub can reach at `/webhook/github`.

## Installation & Setup

For a complete walkthrough, see **[Getting Started with devloop](docs/getting-started.md)**.

```bash
# Clone the repository
git clone https://github.com/omneval/devloop.git
cd devloop

# Install Python dependencies (SDK + dev tooling)
uv sync --all-groups
```

The project consists of:

| Component | Location | Description |
|-----------|----------|-------------|
| **omneval-devloop** | `src/devloop/` | Python SDK — install via `pip install omneval-devloop` or `uv sync` |
| **devloop-agent-base** | `images/agent-base/` | Shared toolchain base image for per-project agents |
| **devloop-agent-universal** | `images/agent-universal/` | Batteries-included agent image (Go, Node, Helm) — the default when a project sets no `agent_image` |
| **devloop-temporal-worker** | `images/temporal-worker/` | Reference Temporal Orchestration Worker image |
| **Helm chart** | `charts/devloop/` | Kubernetes deployment templates |
| **Helper scripts** | `scripts/` | CLI utilities (e.g. `restart_workflows.py`) |

## Quick Start

1. **Expose a webhook endpoint** — follow [Step 1](docs/getting-started.md#step-1-expose-a-webhook-ingress-endpoint) (Cloudflare Tunnel, load balancer, or ngrok).
2. **Install Temporal** — follow [Temporal Prerequisites](docs/temporal-prerequisites.md).
3. **Deploy the Helm chart**:

```bash
helm install devloop charts/devloop/ \
  --set temporalHost=temporal-frontend.agents.svc:7233 \
  --set temporalWorker.agentJob.llm.model="openai/gpt-4o" \
  --set temporalWorker.agentJob.llm.baseUrl="https://your-llm-endpoint" \
  --set temporalWorker.agentJob.llm.apiKey="sk-..." \
  --namespace agents --create-namespace
```

4. **Enroll a project** — create a `projects.yaml` and set it as a ConfigMap:

```yaml
- id: my-project
  github_url: https://github.com/your-org/your-project
  default_branch: main
  # agent_image is optional — omitted, the project runs on the published
  # devloop-agent-universal image (Go, Node, Helm toolchains included).
  agent_label: agent-ready
  omneval_ingest_secret: omneval-ingest-secret
  github_token_secret: devloop-bot-token
```

5. **Verify** — create an issue with the `agent-ready` label in your GitHub repository. The webhook fires immediately; check worker logs:

```bash
kubectl logs -n agents -l app.kubernetes.io/component=temporal-worker --tail=20
```

## Configuration

devloop is configured primarily through Helm values ([`charts/devloop/values.yaml`](charts/devloop/values.yaml)) and environment variables on the temporal-worker pod.

### Helm Values

| Value | Description |
|-------|-------------|
| `temporalHost` | Temporal frontend gRPC address (e.g. `temporal-frontend.agents.svc:7233`). **Required** unless `temporal.enabled=true` |
| `temporal.enabled` | Deploy the official Temporal chart as an evaluation subchart (default `false`) — `temporalHost` then defaults to its frontend Service |
| `temporalWorker.agentJob.llm.model` | LLM model identifier (e.g. `openai/gpt-4o`) |
| `temporalWorker.agentJob.llm.baseUrl` | LLM API base URL (must support `response_format`) |
| `temporalWorker.agentJob.llm.apiKey` | LLM API key (use `apiKeySecret` for production) |
| `temporalWorker.agentJob.llm.roles` | Optional per-role LLM overrides (`review`, `audit`, `extract`) — route the Review phase and the acceptance-criteria audit to a different (e.g. hosted frontier) model than the implementer for cross-model review |
| `temporalWorker.projectsConfigMap` | ConfigMap name/key for the Project Registry file |
| `temporalWorker.maxConcurrentJobs` | Maximum concurrent Agent Execution Jobs (default: `1`) |
| `temporalWorker.ciFixMaxIterations` | Max CI fix loop retries (default: `5`) |
| `temporalWorker.agentJob.networkPolicy.*` | Egress lockdown for Agent Execution Job pods (default on; opt-out via `enabled: false`) — see [Security Model](docs/security-model.md) |
| `githubApp.*` | GitHub App authentication (recommended over PAT) |
| `summarization.*` | Weekly digest schedule and delivery options |

See [docs/getting-started.md](docs/getting-started.md) for the full configuration reference.

## Security

Agent Execution Jobs run code from the enrolled repo with a push-capable
GitHub token — that's the product, and it's also the threat model. The chart
ships a default-on egress NetworkPolicy for agent job pods (DNS, HTTPS, the
Kubernetes API, and your configured LLM/OTLP endpoints; everything else
denied), and **[docs/security-model.md](docs/security-model.md)** documents
what each credential can reach, why branch protection on the default branch
is required, the `agents.homelab/*` pod labels for your own policy engine,
and the GitHub App permission set as the scoping mechanism.

### Environment Variables

| Variable | Description |
|----------|-------------|
| `GITHUB_APP_ID` / `GITHUB_APP_PRIVATE_KEY` / `GITHUB_APP_INSTALLATION_ID` | **Recommended** — GitHub App authentication for the worker (short-lived installation tokens, working reviewer requests). See [docs/github-app.md](docs/github-app.md) |
| `GITHUB_TOKEN` | Per-project token (from `github_token_secret`). Worker-side it's the **fallback** when GitHub App auth is not configured — note a PAT can't deliver formal reviewer requests in single-maintainer setups (GitHub forbids self-review requests). Always mounted into Agent Execution Jobs for `git clone`/`git push` |
| `GITHUB_WEBHOOK_SECRET` | HMAC secret for verifying webhook signatures |
| `AGENT_GITHUB_LOGIN` | GitHub login of the bot account (default: `devloop-bot`) |
| `AGENT_MODEL` | LLM model identifier (forwarded to Agent Execution Jobs) |
| `AGENT_LLM_BASE_URL` | LLM API base URL (forwarded to Agent Execution Jobs) |
| `AGENT_RUNNER` | Agent harness: `openhands` (default) or `claude-agent-sdk` — per-project override via the registry's `agent_runner` field. See [ADR-0011](docs/adr/0011-pluggable-agent-runner.md) |

## Running Tests

```bash
# Install dependencies
uv sync --all-groups

# Lint
uv run ruff check src/ tests/

# Format
uv run ruff format .

# SDK unit tests
uv run pytest tests/

# Agent base image tests
uv run --with openhands-sdk==1.24.0 --with openhands-tools==1.24.0 pytest images/agent-base

# Helm chart tests (requires helm-unittest plugin)
helm unittest charts/devloop

# Helm chart lint
helm lint charts/devloop
```

## Model Endpoint Requirements

### `response_format` with JSON Schema

The structured output extractor (introduced in #53) requires the model endpoint to support
`response_format` with JSON Schema. Without this, the agent cannot produce the structured
outputs needed for plan generation, code editing, and review comments.

**Compatible endpoints:**

| Endpoint | Requirement |
|----------|-------------|
| OpenAI (official) | Native support — no extra flags needed |
| vLLM | Requires the guided decoding backend (e.g. `--enable-guided-decoding` or equivalent). Check the vLLM startup logs for the flag. |
| Ollama | Supported natively on models that expose the response_format parameter |

If your endpoint does not support `response_format` with JSON Schema, the structured
extractor will fall back to unstructured text parsing, which is significantly less
reliable and may cause agent failures.

See [CONTEXT.md](CONTEXT.md) for the full domain glossary and deeper explanation of the
consumer constraint.

### Per-Phase Skill Configuration (`skillsByPhase`)

The `skillsByPhase` Helm value lets you control which skills are available to the agent
during each phase of the Dev Loop. This is set in the `devloop` ConfigMap and forwarded
to Agent Execution Jobs as the `AGENT_SKILLS_BY_PHASE` environment variable.

Key phases include:

| Phase | Purpose |
|-------|---------|
| `plan` | Analysis and planning phase |
| `execute` | Primary implementation phase |
| `remediation` | Runs when the Execute phase produces no commits; the agent revisits the issue with a fresh approach |
| `fix_pass` | Runs after CI checks fail on the PR; the agent iterates to turn red CI green |
| `review` | PR review phase |

**Example Helm values:**

```yaml
skillsByPhase:
  execute: [tdd, code-review]
  remediation: [tdd]
  fix_pass: [tdd]
  review: [code-review]
```

- **Key absent** → all installed skills are available for that phase.
- **Key = `[]`** → no skills available for that phase.
- **Key = list** → exactly those skills are available.

When `remediation` or `fix_pass` are not explicitly configured, they inherit the full set
of installed skills. Set them explicitly if you want to restrict which tools the agent
has during recovery passes.

---

## Troubleshooting: Restarting Stuck Workflows

### Why workflows stop processing open issues

devloop is triggered by GitHub webhook events. When a Dev Loop workflow finishes while open `agent-ready` issues remain in the repository, those issues are silently skipped until a new webhook trigger is received.

Because the webhook uses `WorkflowIDConflictPolicy.USE_EXISTING`, a single POST per project is sufficient to restart the loop: the workflow self-discovers all open `agent-ready` issues on each run.

### Diagnosing

Check the current workflow status with the Temporal admin tools pod:

```bash
kubectl exec -n <namespace> <temporal-admintools-pod> -- \
  temporal workflow list \
  --address temporal-frontend.<namespace>.svc:7233 \
  --namespace default
```

If `devloop-<project-id>` appears as **Completed** or **Failed** but open `agent-ready` issues exist in the repository, the workflow needs a fresh trigger.

Also check the temporal-worker logs to confirm the webhook was received:

```bash
kubectl logs -n <namespace> -l app.kubernetes.io/component=temporal-worker --tail=50
```

### Restarting with the helper script

Port-forward the webhook endpoint to your local machine, then run `scripts/restart_workflows.py`:

```bash
# Open a port-forward in the background
kubectl port-forward -n <namespace> svc/devloop-temporal-worker 8088:8088 &

# Restart all affected projects (GITHUB_TOKEN must have repo:read scope)
uv run scripts/restart_workflows.py \
  --webhook-url http://localhost:8088/webhook/github \
  --repo your-org/your-project \
  --repo your-org/another-project
```

The script fetches open `agent-ready` issues for each repo, skips repos with none, and posts one trigger per project. Preview what would happen without sending requests:

```bash
uv run scripts/restart_workflows.py --dry-run \
  --webhook-url http://localhost:8088/webhook/github \
  --repo your-org/your-project
```

Full option reference:

```
--webhook-url    Temporal worker webhook URL (required)
--repo           GitHub repo in owner/name format; repeat for multiple repos (required)
--label          Agent trigger label (default: agent-ready)
--github-token   GitHub PAT with repo:read scope (falls back to GITHUB_TOKEN env)
--webhook-secret HMAC signing secret (falls back to GITHUB_WEBHOOK_SECRET env;
                 only needed when the worker was deployed with that secret set)
--dry-run        Show what would be triggered without posting
```

### Manual trigger (Temporal CLI)

To trigger directly without the script, exec into the admin tools pod:

```bash
kubectl exec -n <namespace> <temporal-admintools-pod> -- \
  temporal workflow start \
  --workflow-type DevLoopWorkflow \
  --task-queue <task-queue-name> \
  --workflow-id devloop-<project-id> \
  --input '{"project_id": "<project-id>", "agent_label": "agent-ready"}'
```

Replace `<task-queue-name>` with the value from your Helm deployment (the `temporalWorker.taskQueue` value, default `devloop-orchestration`). The `<project-id>` must match the `id` field in your `projects.yaml`.

## Contributing

Contributions are welcome! Here's how to get started:

1. **Fork** the repository and create a feature branch (`git checkout -b feature/my-feature`).
2. **Follow conventions** — review [CONTEXT.md](CONTEXT.md) for domain language, [docs/adr/](docs/adr/) for architecture decisions, and use [uv](https://github.com/astral-sh/uv) for Python dependency management (no `requirements.txt`).
3. **Run tests** before pushing: `uv run ruff check src/ tests/ && uv run pytest tests/`.
4. **Use Conventional Commits** — prefix messages with `feat:`, `fix:`, `chore:`, `docs:`, or `test:`.
5. **Open a Pull Request** against `main` with a clear description of the change.

## License

This project is licensed under the [Apache License, Version 2.0](LICENSE).
