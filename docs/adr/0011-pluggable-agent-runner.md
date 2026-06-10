# ADR-0011: Pluggable agent runner seam (AGENT_RUNNER)

## Status

Accepted (issue #121)

## Context

`run_agent` in `images/agent-base/entrypoint.py` is the single seam every
test mocks, but the harness driving the model was hard-wired inside it: the
OpenHands SDK (`LLM` → `build_agent` → `LocalConversation`), plus
OpenHands-specific patches (the lmnr `TracerManager` service-name fix, the
hand-rolled `build_agent` from ADR-0007). devloop's bring-your-own-endpoint
pitch deserved a first-class way to choose the harness too: users with an
Anthropic API key get dramatically better results from a frontier model
driven by its **native** harness than through a generic tool-calling shim,
and small local models often do better with simpler harnesses than with
tool-calling at all.

## Decision

A runner contract in `images/agent-base/runners.py`, selected by the
`AGENT_RUNNER` environment variable:

```
runner = runners.resolve_runner()            # AGENT_RUNNER, default "openhands"
session = runner.start(spec, workdir, skills=..., build_agent=..., llm_setting=...)
text = session.send(message)                 # one agent turn
text = session.send(resume_prompt)           # same conversation — QUESTION: round-trip
```

- **What moved**: only the harness block — LLM/agent/conversation
  construction and the run-and-collect-final-text turn. The OpenHands code
  moved verbatim into `OpenHandsRunner`.
- **What stayed in `entrypoint.py`** (harness-portable): phase handlers,
  prompt rendering, the `QUESTION:` / `ANSWER:` / `<promise>COMPLETE</promise>`
  sentinels, commit/push logic, structured extraction, skill resolution, and
  the stub fast-path.
- **Sessions own conversation continuity**: the mid-run question round-trip
  resumes by calling `send` again on the same session. OpenHands keeps the
  `LocalConversation` in process; the Claude runner resumes via the session
  id captured from the previous turn's `ResultMessage`.
- **Dependency injection instead of imports**: `build_agent` (the ADR-0007
  override seam derived images monkeypatch) and `_llm_setting` (per-role env
  resolution) are passed into `runner.start` at call time. `runners.py`
  cannot import the entrypoint back — it runs as `__main__` in the image —
  and injection picks up image-level overrides automatically.

### Selection precedence

1. Project Registry `agent_runner` field (per-project),
2. `AGENT_RUNNER` env on the worker (Helm `temporalWorker.agentJob.runner`,
   deployment-wide),
3. `openhands` default.

The worker forwards the resolved value into each Agent Execution Job
(`k8s_jobs.render_job`). An unknown runner name fails loudly rather than
silently falling back — a misconfigured A/B comparison must not lie.

### Capability matrix

| Capability | `openhands` (default) | `claude-agent-sdk` |
|---|---|---|
| Model endpoints | any OpenAI-compatible (`AGENT_LLM_BASE_URL`) | Anthropic models only (API key, Bedrock, Vertex) |
| Agent Skills injection | yes (`AgentContext`) | not yet — resolved skills are ignored with a warning |
| Context condenser | yes (LLM-summarising condenser) | SDK-managed (automatic compaction) |
| Mid-run pause/resume (`QUESTION:`) | in-process conversation | session-id resume (`options.resume`) |
| Per-role LLM routing (review) | yes | model only (`AGENT_MODEL_REVIEW`) |
| Image requirements | agent-base (bundled) | derived image with `claude-agent-sdk` + Claude Code CLI |
| Credentials | `AGENT_LLM_API_KEY` | `AGENT_LLM_API_KEY` → exported as `ANTHROPIC_API_KEY` |

### Non-goals

- Rewriting the workflow or replacing OpenHands as the default. Runner swaps
  should be **measured** (the issue #122 eval flywheel: KPI span attributes
  + `devloop-bench`) before any default changes.
- A third minimal runner (bash-only ReAct, mini-swe-agent-style) for small
  local models — where simpler harnesses often beat tool-calling because
  tool-call parsing is small models' weakest skill — is anticipated by the
  contract (it's one class with `start`/`send`) but deliberately not shipped
  until there's a bench result to justify it.

## Consequences

- The OpenHands path is behavior-compatible behind the seam: the existing
  `test_run_agent.py` fake-SDK suite passes unmodified (the fake
  `openhands.sdk` modules are imported at the same points, now inside
  `OpenHandsRunner`).
- `runners.py` joins `skills.py` in the `/usr/local/bin` placement contract
  (bare sibling import from the entrypoint; missing COPY fails every test).
- The claude-agent-sdk runner needs a derived image; agent-base/universal do
  not bundle the Claude Code CLI. Until then selecting it in a stock image
  fails at first turn with a clear import error in the phase summary.
