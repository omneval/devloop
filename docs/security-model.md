# Security Model

devloop's Agent Execution Jobs run **arbitrary code from the enrolled
repository** — install scripts, test suites, and whatever commands the agent
itself decides to run — inside your cluster. That is the product working as
designed, and it is also the threat model. This page spells out exactly what
an agent job holds, what it can reach, the defaults that contain it, and the
controls you are expected to add on the GitHub side.

This posture matters most when you enroll repositories you don't fully
control (public repos with outside contributors): an issue body, a README, or
a test fixture in the cloned repo are all attacker-controlled inputs to the
agent.

## What an Agent Execution Job holds

Each job pod is created by the temporal-worker (`k8s_jobs.render_job`) with:

| Credential / access | Source | Blast radius |
|---|---|---|
| `GITHUB_TOKEN` / `GH_TOKEN` | the project's `github_token_secret` | clone + **push any branch** of the repos the token is scoped to (see [Branch protection](#branch-protection-is-required)) |
| `OMNEVAL_API_KEY` | the project's `omneval_ingest_secret` | write traces into your omneval project (pollution, not exfiltration — the key is ingest-only) |
| `AGENT_LLM_API_KEY` (+ per-role variants) | chart values / Secret | spend against your LLM endpoint |
| Kubernetes ServiceAccount | chart-created `<release>-agent-job` | ConfigMaps in the release namespace only: `create/get/update/patch` (the job's result-reporting channel) — no pods, no secrets, no jobs |
| Network egress | pod network | restricted by the chart's egress NetworkPolicy (below); unrestricted only if you opt out or your CNI doesn't enforce NetworkPolicy |

The worker pod (not the agent job) additionally holds the GitHub App private
key or webhook secret if configured — agent jobs never see those.

## Egress NetworkPolicy (default on)

The chart templates a deny-by-default egress NetworkPolicy for every agent
job pod (`temporalWorker.agentJob.networkPolicy.enabled: true`, opt-out).
Agent job pods are selected by the `agents.homelab/project` label; allowed
egress is:

- **DNS** — 53/UDP + 53/TCP
- **HTTPS (443/TCP)** — GitHub, package registries, and any https LLM/OTLP
  endpoint
- **Kubernetes API (6443/TCP)** — the result-ConfigMap writes (API servers
  fronted on 443 are covered by the HTTPS rule)
- **Configured endpoint ports** — every port parsed from
  `temporalWorker.agentJob.llm.baseUrl`, each `llm.roles.*.baseUrl`, and
  `temporalWorker.agentJob.networkPolicy.otlpEndpoint` (which must mirror the
  worker's `OTEL_EXPORTER_OTLP_ENDPOINT`; the default matches the worker's
  built-in omneval default)

Everything else — other cluster Services, the node network, arbitrary
internet ports — is denied.

Two honest limitations:

1. **A vanilla NetworkPolicy is L3/L4.** GitHub publishes no stable CIDRs,
   so "the GitHub endpoints" are approximated as *any* host on 443. That
   still cuts off non-HTTPS exfiltration, lateral movement to in-cluster
   Services on other ports, and any C2 channel that isn't TLS-on-443, but it
   does not pin hostnames. If you need FQDN-level pinning
   (`*.github.com` and your LLM host only), use a CNI policy engine such as
   Cilium — see the pod labels below.
2. **NetworkPolicy is enforced by the CNI.** On clusters whose CNI does not
   implement NetworkPolicy the rendered policy is inert. Verify with your
   CNI's documentation.

Use `temporalWorker.agentJob.networkPolicy.extraEgress` to append rules
(HTTP-only package mirror, nonstandard API server port) instead of disabling
the policy outright.

## Pod labels for your own policy engine

Every agent job pod carries:

| Label | Value |
|---|---|
| `agents.homelab/project` | the Project Registry `id` the job runs for |
| `agents.homelab/phase` | the Dev Loop phase (`plan`, `execute`, `ci_fix`, `review`, `pr_comment`, `answer`, …) |

These are stable selector surfaces for Cilium/Calico policies, OPA/Kyverno
rules, or audit queries — e.g. a per-project Cilium FQDN allowlist, or a
stricter policy for `execute` (which runs repo install scripts) than for
`plan`.

The Job object (not the pod) additionally carries
`app.kubernetes.io/managed-by: orchestration-worker`.

## Branch protection is required

devloop **never merges** — the Dev Loop ends at an open PR for a human to
review. But the per-project token can *push*, and a fine-grained PAT's
Contents read/write permission cannot be scoped to specific branches. A
malicious instruction that makes its way into the agent's context could
therefore push directly to `main` unless you prevent it. Before enrolling a
repository:

- **Protect the default branch** (Settings → Branches → branch protection
  rule, or a ruleset): require a pull request before merging, and do not
  exempt the bot. This turns "the token can push anywhere" into "the token
  can push `agent/*` branches that still need human review".
- Require review from someone other than the bot, so a pushed branch can't
  be self-approved.

## GitHub permission scoping

The [GitHub App path](github-app.md) is the scoping mechanism for everything
the *worker* does on GitHub (PRs, comments, reviewer requests): exactly
Contents rw, Pull requests rw, Issues rw, Checks read, Workflows rw —
installable per-repository, with 1-hour installation tokens minted on demand.

The per-project `github_token_secret` mounted into agent jobs for git
clone/push should be scoped to **only the enrolled repositories** (a
fine-grained PAT, not a classic one) and, when the GitHub App handles the
worker's API calls, narrowed to Contents + Workflows read/write.

## Webhook ingress

The single inbound surface is `/webhook/github` on the worker. Always set
`GITHUB_WEBHOOK_SECRET` (Step 2/6 of [getting-started](getting-started.md))
so every delivery is verified against its `X-Hub-Signature-256` HMAC before
it is parsed; unsigned or mis-signed deliveries are rejected. The webhook
receiver also filters out the bot's own comments/reviews
(`AGENT_GITHUB_LOGIN`) so devloop can't be made to re-trigger itself.

## Worker RBAC

The chart-created worker ServiceAccount is namespace-scoped (Role, not
ClusterRole): batch Jobs (create/get/list/watch/delete), ConfigMaps,
pods/pods-log read, and Secret **get/list** (it resolves the per-project
token and ingest Secrets at dispatch time). Treat the release namespace as
the devloop trust boundary: don't co-locate unrelated Secrets in it, since
the worker can read any Secret in its namespace.
