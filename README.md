# devloop
Generic Autonomous AI Workflow for improving codebases

See [docs/getting-started.md](docs/getting-started.md) for full setup instructions.

---

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
