# Alert Response Workflow — Consumer Extension Example

This example shows how to extend **omneval-devloop** with a custom Temporal
workflow.  The pattern is:

1. Install `omneval-devloop` from PyPI in your own project.
2. Write a custom workflow (e.g. `AlertResponseWorkflow`).
3. Register **both** the SDK workflows and your custom workflow in a single
   Temporal worker process.
4. Your workflow reuses SDK activities (`dispatch_agent_job`,
   `post_github_comment`, etc.) so you get Kubernetes Job dispatch and
   GitHub-comment notifications for free.

## The Alert Response Workflow pattern

```
┌─────────────┐    webhook     ┌───────────────────┐
│ AlertManager │ ─────────────► │ AlertResponseWF   │
└─────────────┘                │                   │
                               │  1. diagnose      │──► Agent Job (K8s)
                               │  2. check allowlist│──► allowlist.yaml
                               │  3. execute / ask  │──► GitHub comment approval
                               │  4. notify          │──► summary
                               └───────────────────┘
```

| Step | What happens |
|------|-------------|
| **Diagnose** | An Agent Job runs on Kubernetes to understand the alert |
| **Allowlist check** | Each suggested remediation is checked against `allowlist.yaml` |
| **Execute** | Allowlisted actions run autonomously via Agent Jobs |
| **Approve** | Non-allowlisted actions pause for a human reply on the GitHub Issue |
| **Notify** | A summary is posted as a GitHub comment |

## File layout

```
alert-response/
├── Dockerfile              # Consumer image: uv + omneval-devloop + custom code
├── pyproject.toml          # Declares omneval-devloop as a dependency
├── uv.lock                 # Reproducible lockfile (committed)
├── worker.py               # Registers DevLoopWorkflow + AlertResponseWorkflow
├── alert_response.py       # Custom workflow implementation
└── allowlist.yaml          # Pre-approved actions (mounted into the pod)
```

## Running locally

```bash
cd docs/examples/alert-response
uv sync
export TEMPORAL_HOST=localhost:7233
export PROJECTS_FILE=../../projects.yaml
python worker.py
```

## Adapting the example

To create your own custom workflow:

1. **Write your workflow** — create a file like `my_workflow.py` with a
   `@workflow.defn` class.  Import activities and types from `devloop`:

   ```python
   from temporalio import workflow
   from devloop.shared import DispatchInput, TaskSpec, AgentJobResult

   @workflow.defn
   class MyCustomWorkflow:
       @workflow.run
       async def run(self, inp):
           result = await workflow.execute_activity(
               "dispatch_agent_job",
               DispatchInput(...),
               result_type=AgentJobResult,
           )
           # ... handle result
   ```

2. **Register in worker** — add your workflow class to the `WORKFLOWS` list in
   `worker.py`.  Reuse the same `ACTIVITIES` list from the SDK.

3. **Configure** — update `pyproject.toml` with any additional dependencies
   your workflow needs.  Run `uv lock` to regenerate the lockfile.

4. **Build** — update `Dockerfile` to `COPY` your workflow code and any
   configuration files.

## Building the Docker image

```bash
docker build -t ghcr.io/your-org/alert-response-worker:latest .
```

The image expects a `projects.yaml` volume mount at runtime (via a Kubernetes
ConfigMap).

## Deploying

The Helm chart already supports custom images — set
`temporalWorker.image.repository` to your built image.  The worker will handle
both the built-in DevLoop workflows and your custom ones on the same task queue.
