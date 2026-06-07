# devloop

An open-source framework that packages the Dev Loop engine so any team can run autonomous, agent-driven code improvement workflows on their own Kubernetes cluster. Ships two container images, the `omneval-devloop` Python SDK, and a Helm chart. Temporal is a documented prerequisite that consumers bring independently.

## Language

**Dev Loop**:
The multi-phase autonomous workflow for maintaining and improving an enrolled codebase. Phases run in order: Plan → Execute (with an embedded [CI Fix Loop](#ci-fix-loop)) → Review → Fix Pass (if `needs_fixes`) → reviewer notification. There are no human-approval gates and devloop never merges — it opens/updates the PR, requests a GitHub reviewer, and posts a GitHub Issue comment summarising the result for a human to review and merge. Triggered by the `agent-ready` label being applied to a GitHub issue. One issue is processed per round; the loop repeats until no unblocked issues remain.
_Avoid_: agent pipeline, CI loop, autonomous CI

**Planner**:
The first phase of the Dev Loop. An OpenHands agent that reads all open `agent-ready`-labeled issues for the triggering project, scoped to the issue that started the run, and builds a dependency-ordered execution plan. The plan is consumed directly by `_execute_phase` — there is no approval step before code is written.
_Avoid_: planning agent, issue sorter

**Summarization Agent**:
A Temporal workflow run on a configurable schedule (`summarization.cronSchedule`, disabled by default). Reads the git diff and closed issues since the last run, generates a plain-English explanation of what changed and why, opens (or appends to) a `devloop-summary`-labelled GitHub Issue via the `publish_summary` activity, and optionally POSTs the digest as JSON to `SUMMARIZATION_WEBHOOK_URL` (fire-and-forget) for consumers who want to bridge it elsewhere (Slack, Discord, etc.).
_Avoid_: changelog agent, diff summarizer

**Project Registry**:
A YAML config file (owned by the consumer, typically `agents/projects.yaml` in their GitOps repo) enumerating all repos enrolled for Dev Loop management. Each entry declares: GitHub repo URL, agent image reference, default branch, `agent-ready` label name, omneval ingest secret name, and GitHub token secret name. Adding a project is a change to the consumer's repo — no dynamic registration.
_Avoid_: agent config, project database

**Agent Base Image**:
The container image (`ghcr.io/omneval/devloop-agent-base`) used as the `FROM` base for all per-project agent images. Contains the shared toolchain: OpenHands SDK, `omneval-devloop` (for the shared Agent Job output ConfigMap protocol and its pinned Temporal + kubernetes clients), git, gh CLI, kubectl, flux CLI, argocd CLI. Per-project images extend it with only the language runtime and prompts they need.
_Avoid_: base container, shared agent image

**Agent Job output ConfigMap**:
The Kubernetes ConfigMap an Agent Execution Job writes its result to and reads a human's mid-run reply from — the message-bus seam between the Job and the Temporal Orchestration Worker. The agent writes the JSON-encoded result under the `result` key (`AgentJobResult.to_payload`); the worker polls and rebuilds it (`AgentJobResult.from_payload`). A blocking question parks the Job and the worker patches the answer back under the `human_answer` key. The contract (field set and key names) is owned once in `devloop.shared` so both `devloop-temporal-worker` and `devloop-agent-base` reference one definition.
_Avoid_: result ConfigMap, status ConfigMap, output map

**Agent Execution Job**:
A Kubernetes `batch/v1 Job` spawned by the Temporal Orchestration Worker for each Execute or Review phase. Each Job runs a single-use Temporal Activity Worker, processes one agent task via OpenHands SDK with `LocalWorkspace`, then exits. The pod is the isolation boundary — no Docker-in-Docker. The Job image is per-project, pulled from the Project Registry entry.
_Avoid_: agent pod, worker job, sandbox job

**Temporal Orchestration Worker**:
The long-running Kubernetes Deployment that hosts Temporal Activity Workers for lightweight activities: planning, GitHub API calls (comments, reviewer requests, CI-check polling, App-token minting), webhook ingestion, and Agent Execution Job spawning. The `devloop-temporal-worker` reference image runs this using only `omneval-devloop`. It is the sole running deployment — `devloop-agent-base` is a build-time-only base image. Consumers who need additional workflows (e.g. a homelab Alert Response Workflow) build their own image that installs `omneval-devloop` and registers their custom workflows alongside.
_Avoid_: Temporal worker pod, orchestration service

**omneval-devloop**:
The Python package (`pip install omneval-devloop`, PyPI name `omneval-devloop`, import as `import devloop`) that contains the reusable Dev Loop workflow logic: `DevLoopWorkflow`, `SummarizationWorkflow`, `k8s_jobs`, `projects`, `github_ops`, `shared` dataclasses, and activity implementations. Consumers import it to register the Dev Loop workflows alongside their own custom Temporal workflows without forking the devloop repo.
_Avoid_: devloop-sdk, devloop library, agent SDK

**devloop Consumer**:
Any deployment that installs `omneval-devloop` and runs it against one or more enrolled codebases. A consumer owns its Project Registry, per-project agent images, and deployment configuration. Consumers who need custom Temporal workflows (beyond the Dev Loop) build their own Temporal Orchestration Worker image that installs `omneval-devloop` alongside their custom workflow code.
_Avoid_: devloop user, devloop instance

**devloop images**:
The two container images published to `ghcr.io/omneval/` by this repo: `devloop-agent-base` (shared toolchain base, build-time only — not a running deployment) and `devloop-temporal-worker` (reference Temporal Orchestration Worker, the sole running deployment). Image tags follow `sha-<7-char-hash>-<unix-epoch>` for main builds and semver for releases.
_Avoid_: devloop containers, agent images (too generic)

**Agent Skill**:
A reusable, model-agnostic capability in the AgentSkills `SKILL.md` format (YAML frontmatter — `name`, `description`, optional OpenHands-only `triggers:` — plus a markdown body, optionally with `scripts/` `references/` `assets/`). Loaded by the OpenHands agent with native progressive disclosure: a skill's name/description appears in `<available_skills>` and the agent reads the full body on demand via `invoke_skill()`, so a skill costs almost no context until used. The same format the `npx skills` ecosystem publishes (agentskills.io). Distinct from a Phase prompt template (always rendered, one per phase) — a skill is conditionally surfaced and shared across phases.
_Avoid_: plugin, tool, microagent, prompt template

**Skills convergence directory**:
The single on-image directory where every Agent Skill resolves regardless of how it was delivered (`/usr/local/share/agent-skills/installed`, overridable via `AGENT_SKILLS_DIR`). Skills baked into the Agent Base Image or a per-project image sit here directly; skills delivered at deploy time via a Helm-managed ConfigMap are mounted to a separate read-only staging path and installed into this directory by the entrypoint at pod start (ConfigMap wins on name collision). The agent loads the merged set once via `load_installed_skills()`. A volume mount cannot target this directory directly — it would hide the baked skills — which is why ConfigMap skills are staged-and-installed, not mounted in place.
_Avoid_: skills folder, skills mount, skills volume

**Skill triggers**:
Keywords declared in a skill's `SKILL.md` frontmatter (`triggers:` list) that gate whether a skill surfaces to the agent. In the default `"triggers"` selection mode, a skill is only presented to the agent when the conversation context matches at least one of these keywords, keeping context overhead low.
_Avoid_: skill keywords, activation conditions, trigger words

**Selection mode**:
Controls how eligible skills are presented to the agent within a phase: `"triggers"` (default) surfaces a skill only when the conversation matches its `triggers:` frontmatter; `"advanced"` surfaces all phase-eligible skills so the model selects the most appropriate one autonomously. Configured via the `skillsSelectionMode` Helm value and forwarded to each Agent Execution Job as `AGENT_SKILLS_SELECTION_MODE`.
_Avoid_: skill discovery mode, skill matching mode

**Phase.ANSWER**:
An Agent Execution Job phase that answers a paused agent's mid-run clarifying question (`AWAITING_HUMAN`) without any human involvement: a fresh agent investigates the question with read/write access to the working branch and returns its best-informed decision, which the workflow patches back into the paused job's ConfigMap via `answer_agent_job` so it can resume via `await_agent_job`. Dispatched on the [job dispatch queue](#temporal-orchestration-worker) (counts against `maxConcurrentJobs`) and bounded by `max_questions_per_phase` (Helm `temporalWorker.maxQuestionsPerPhase`, env `MAX_QUESTIONS_PER_PHASE`, default 3): once a phase run hits that many questions, the workflow stops spawning answer jobs and tells the parked agent to proceed with its best guess directly. Replaces the earlier Discord-mediated human-reply loop (`question_timeout_seconds` / `QUESTION_TIMEOUT_SECONDS`, removed).
_Avoid_: auto-answer phase, question resolver, answer agent

**Per-phase enablement**:
Operator-controlled allowlist of skill names available in each Dev Loop phase (plan, execute, review, diagnosis, ci_fix, fix_pass). Configured via the `skillsByPhase` Helm value and propagated to each Agent Execution Job as `AGENT_SKILLS_ENABLED`. Three-way semantics: phase key absent means all installed skills are available; `[]` means no skills for that phase; a name list means exactly those skills are loaded.
_Avoid_: skill allowlist, skill whitelist, phase skill filter

**CI Fix Loop**:
The Dev Loop loop (`Phase.CI_FIX`, driven by `DevLoopWorkflow._ci_fix_loop`) that runs inside the Execute phase, after the implementation Agent Execution Job pushes commits and before Review. Each iteration polls the PR's CI checks via the `poll_ci_checks` activity (GitHub Checks API, the `gh pr checks` equivalent); if every check has passed, the loop exits early. Otherwise it posts a "⏳ queued" GitHub Issue comment and dispatches a `Phase.CI_FIX` Agent Execution Job — carrying the current failing-check details in `TaskSpec.extra["ci_check_failures"]` — which clones the issue branch and makes a minimal, targeted attempt to turn CI green, then posts a "🔧 CI fix attempt {N}/{max}" (or "❌ … failed") result comment. The loop retries up to `ci_fix_max_iterations` (`CI_FIX_MAX_ITERATIONS` env / `temporalWorker.ciFixMaxIterations` Helm value, default 5) times. If every attempt is spent without CI going green, `_ci_fix_loop` returns `exhausted=True`; `_execute_phase` carries that flag through to `_notify_reviewer`, which appends a "CI still failing" note to the reviewer notification rather than blocking the round. This replaces the never-implemented single-attempt Remediation phase (`Phase.REMEDIATION`, removed). Distinct from the Fix Pass — CI Fix targets CI check failures, Fix Pass targets reviewer findings.
_Avoid_: Remediation phase, remediation loop, check fix agent, CI remediation agent

**Structured phase output**:
The mechanism by which Dev Loop phases produce their structured conclusions. Because Agent Execution Jobs run OpenHands `LocalConversation` — a multi-step tool-use loop that makes many LLM calls internally — `response_format` cannot be applied to the loop itself. Instead, after `conversation.run()`, a second direct LLM call is made against the same endpoint using `response_format` and a Pydantic `BaseModel` to extract the structured conclusion from the agent's raw summary text. This replaces the fragile `<tag>`-based extraction (`_extract_plan`, `_extract_review`, `_extract_diagnosis`). **Consumer constraint**: the model endpoint (configured via `AGENT_MODEL` / `AGENT_LLM_BASE_URL`) must support `response_format` with JSON schema — any OpenAI-compatible endpoint with guided generation (vLLM with `--guided-decoding-backend`, Ollama, hosted OpenAI/Anthropic) satisfies this. Endpoints that do not support `response_format` will cause the extraction call to fail.
_Avoid_: tag parsing, regex extraction, structured output, JSON extraction

**Review verdict**:
The three-state outcome the Review phase emits after analysing the diff and posting PR comments: `lgtm` (no changes needed, proceed straight to the reviewer notification), `needs_fixes` (agent-fixable issues found, trigger the Review Fix Pass), or `needs_human` (changes require human judgement, park the issue with a GitHub Issue comment). Encoded in `AgentJobResult.review` alongside the existing `summary` and `inline_comments` fields.
_Avoid_: review result, review status, review decision

**Fix Pass**:
The Dev Loop phase (`Phase.FIX_PASS`) triggered when the Review phase returns a `needs_fixes` verdict. An Agent Execution Job clones the issue branch, retrieves all existing PR comments (review summary, inline comments, CI failure notes) via `gh pr view --comments`, and attempts to resolve every outstanding issue in one shot. A failed attempt is defined as: `commits == 0` or `status != complete`. On failure, the issue is parked identically to the `needs_human` path — a GitHub Issue comment, move on to the next issue.
_Avoid_: post-review fix agent, second review, fix iteration, Review Fix Pass

---

## Conventions

**Model endpoint requirement**: The `AGENT_LLM_BASE_URL` endpoint must support `response_format` with JSON schema (OpenAI structured outputs). Any OpenAI-compatible endpoint with guided generation satisfies this: vLLM (`--guided-decoding-backend outlines` or `lm-format-enforcer`), Ollama, hosted OpenAI, hosted Anthropic (via the `openai`-compatible shim). Endpoints that reject `response_format` will cause structured phase output extraction to fail. Document this requirement when onboarding a new model endpoint.

**Python tooling**: Always use [uv](https://github.com/astral-sh/uv) for Python dependency management. Initialise packages with `uv init`. Define dependencies in `pyproject.toml`; do not use `requirements.txt`. Commit `uv.lock`. In Dockerfiles, copy uv from the official image and install with `uv pip install --system --no-cache .`:

```dockerfile
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /bin/
COPY pyproject.toml uv.lock .
RUN UV_HTTP_TIMEOUT=300 uv pip install --system --no-cache .
```

Publish packages to PyPI with `uv build` + `uv publish` (OIDC trusted publisher — no stored API token).

**Image tag format**: `sha-<7-char-hash>-<unix-epoch>` for builds from main; semver (`v1.2.3`) for releases. The epoch component allows FluxCD ImagePolicy to select the newest build by alphabetical ordering without requiring semver on every commit.

---

## Architecture decisions

- **ADR-0003** (from `home-server`): Temporal is the durable orchestration layer. OpenHands SDK and Agent Execution Jobs are called as activities from within Temporal workflows.
- **ADR-0004** (from `home-server`): Agent Execution Jobs use Kubernetes Jobs + OpenHands `LocalWorkspace` — no Docker-in-Docker. The pod is the isolation boundary.
- **ADR-0005** (from `home-server`): OpenHands SDK replaced Pi/Sandcastle for stuck detection, built-in OTLP tracing, and native pause/resume.
- **ADR-0006** (from `home-server`): Dev Loop core is extracted as the `omneval-devloop` Python package rather than a plugin/extension mechanism, giving consumers a stable, testable API surface with version mismatches caught at install time.
- **ADR-0007**: `get_default_agent` is replaced with hand-rolled `Agent(...)` construction (`build_agent` in `entrypoint.py`) to gain the `agent_context` parameter needed for Agent Skills injection. The function is also the override seam for consumers who need custom tools.
- **ADR-0008**: Agent Skills use a convergence directory with stage-and-install for ConfigMap delivery. Mounting the ConfigMap directly at the convergence directory would hide baked skills; instead the ConfigMap is mounted at a staging path and the entrypoint installs into the convergence directory at pod start.
- **ADR-0009** _(superseded by ADR-0011; its re-triggering goal is now implemented)_: The polling-based trigger mechanism has been replaced by webhook ingress — devloop now requires a public-facing webhook endpoint, and GitHub delivers `issues`, `pull_request_review`, and `issue_comment` events directly to the temporal-worker at `/webhook/github`. The automated re-triggering this ADR called for has landed as `PRCommentWorkflow`: a human's PR review or `@devloop-bot` comment on an open agent PR re-engages the agent on the existing branch (queued comment → `Phase.PR_COMMENT` job → CI Fix Loop → re-request reviewer), no Argo Events needed. Issues parked by the `needs_human` Review verdict still require a human to act on GitHub directly (re-running Plan/Execute is out of scope for this path).
- **ADR-0010**: Structured phase output uses a post-processing LLM extraction call rather than `<tag>`-based regex parsing. OpenHands `LocalConversation` drives a multi-step tool-use loop; `response_format` cannot be applied to a loop, only to a single API call. After the loop finishes, a second direct call with `response_format` and a Pydantic `BaseModel` extracts the structured conclusion. Trade-off: one extra LLM call per phase (plan, review, diagnosis) in exchange for eliminating fragile tag/regex parsers and gaining Pydantic validation. This introduces a hard consumer requirement: the model endpoint must support `response_format` with JSON schema.
