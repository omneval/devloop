# devloop
Generic Autonomous AI Workflow for improving codebases

See [docs/getting-started.md](docs/getting-started.md) for full setup instructions.

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
