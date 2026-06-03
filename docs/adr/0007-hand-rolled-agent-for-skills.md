# ADR-0007: Replace `get_default_agent` with hand-rolled `Agent(...)` construction

**Status**: Accepted
**Date**: 2026-06-03
**Issues**: #32 (skill resolution module and `build_agent` seam)

---

## Context

The Agent Execution Job entrypoint used `get_default_agent(llm=llm, cli_mode=True)` from
`openhands.tools.preset.default` to build the OpenHands agent. This convenience preset wires
the standard tools (terminal, file_editor, task_tracker) and a summarising condenser, and was
the correct choice when the entrypoint had no per-Job configuration to inject.

Introducing Agent Skills (issues #32–#36) requires passing an `AgentContext(skills=...)` to
the `Agent(...)` constructor. `get_default_agent` does not accept an `agent_context` parameter
— it constructs `Agent` internally without exposing this argument.

Alternatives considered:

1. **Monkey-patch `get_default_agent`**: fragile; couples us to internal SDK naming.
2. **Post-construction injection**: `AgentContext` is not a mutable attribute after construction.
3. **Subclass `Agent`**: adds coupling to the SDK's inheritance hierarchy with no benefit.
4. **Hand-roll the `Agent(...)` construction (chosen)**: replicates the preset inline, gaining
   the `agent_context` parameter while preserving the preset's tool and condenser choices.

---

## Decision

Replace the `get_default_agent(...)` call with a `build_agent(llm, cli_mode, agent_context)`
function in `entrypoint.py` that replicates the preset's behaviour:

```python
def build_agent(llm, cli_mode: bool = True, agent_context=None):
    from openhands.sdk import Agent
    from openhands.tools.preset.default import get_default_condenser, get_default_tools

    tools = get_default_tools(enable_browser=not cli_mode)
    condenser = get_default_condenser(
        llm=llm.model_copy(update={"usage_id": "condenser"})
    )
    return Agent(
        llm=llm,
        tools=tools,
        system_prompt_kwargs={"cli_mode": cli_mode},
        condenser=condenser,
        agent_context=agent_context,
    )
```

`get_default_tools` and `get_default_condenser` are the same helpers the preset calls
internally. `agent_context=None` is the no-op path: the agent is built identically to the
old `get_default_agent(...)` call when no skills are loaded.

`build_agent` is the **override seam** for consumers who extend the Agent Base Image: a
derived image can replace this function to inject custom tools or a different condenser without
changing the rest of the entrypoint.

---

## Consequences

**Accepted cost**: the replicated construction can drift from openhands-ai's default preset
across SDK upgrades (e.g. if the tool set or condenser algorithm changes). This is treated as
a known maintenance cost: when bumping the openhands-ai version, re-check the preset source
against `build_agent`.

**Do not revert**: a future reader should not "simplify" this back to `get_default_agent`. The
hand-rolled construction is the intentional implementation — it is the only path that supports
`agent_context` injection, and the comment in `entrypoint.py` explains this.

**Backward compatibility**: `agent_context=None` means the agent is constructed identically to
the pre-skills baseline. Deployments with no skills installed take the no-op path transparently.
