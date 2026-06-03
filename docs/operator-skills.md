# Agent Skills — Operator Guide

This guide covers the Helm values that control Agent Skills in a devloop deployment.

---

## Overview

Agent Skills extend the OpenHands agent's capabilities within each Agent Execution Job. Two
delivery paths are designed:

| Path | Status | When to use |
|------|--------|-------------|
| **Baked into a per-project agent image** (recommended) | Dockerfile `COPY` step pending wiring | Domain-specific skills that need `scripts/`, `assets/`, or multi-file structure. Requires an image rebuild to change. |
| **Helm ConfigMap** (global supplement) | Not yet wired (Helm template + entrypoint call pending) | Single-file `SKILL.md` overrides or additions that apply across projects without a rebuild. Limited to 1 MiB total, flat `key → content` structure only. |

Per-phase allowlist control (`skillsByPhase`) and selection mode (`skillsSelectionMode`) are
**fully wired** in this release — they take effect for any skills already present in the
convergence directory (e.g. placed there manually or by a pre-existing image layer).

At pod startup the entrypoint loads all skills from the **skills convergence directory**
(`/usr/local/share/agent-skills/installed`, overridable via `AGENT_SKILLS_DIR`). The per-phase
allowlist then filters to exactly the skills permitted for the active phase.

---

## Helm values reference

### `skillsByPhase`

Per-phase allowlist of skill names that Agent Execution Jobs may load.

**Type**: map of `phase → list[string]`
**Default**: `{}` (all installed skills available for all phases)

| Configuration | Effect |
|---------------|--------|
| Phase key absent | All installed skills are available for that phase |
| Phase key = `[]` | No skills available for that phase |
| Phase key = `[name, ...]` | Exactly those installed skills are available |

Example — restrict skills by phase:

```yaml
skillsByPhase:
  plan: []                             # planner runs without skills
  execute: [tdd, code-review]          # implementer gets two skills
  review: [code-review, security-review]
  # merge and diagnosis keys absent → all installed skills available
```

To disable skills for all phases:

```yaml
skillsByPhase:
  plan: []
  execute: []
  review: []
  merge: []
  diagnosis: []
```

---

### `skillsSelectionMode`

Controls how the agent discovers and selects from its eligible skills.

**Type**: string
**Default**: `"triggers"`
**Valid values**: `"triggers"`, `"advanced"`

| Value | Behaviour |
|-------|-----------|
| `"triggers"` | Keyword-driven: a skill surfaces only when the conversation context matches the skill's `triggers:` frontmatter. Lowest context noise. |
| `"advanced"` | Model-driven: all phase-eligible skills are surfaced; the model selects the most appropriate one autonomously. Use when trigger matching is too narrow. |

Example:

```yaml
skillsSelectionMode: "advanced"
```

---

### `skills` (ConfigMap delivery — not yet wired)

**Status**: The `install_configmap_skills` function is implemented in
`images/agent-base/skills.py`, but the Helm `skills:` value, the
`agent-skills-configmap.yaml` chart template, and the entrypoint `main()` call to
`install_configmap_skills` are not yet wired on this branch.

When wired, `skills:` will accept a map of `skill-name → SKILL.md content`. Each key becomes
one skill available to all Agent Execution Jobs. ConfigMap-delivered skills win on name
collision — they override a same-named baked skill.

**Constraint**: values must be complete, single-file `SKILL.md` documents. `scripts/`,
`assets/`, and `references/` subdirectories are not supported through ConfigMap delivery
(a ConfigMap is a flat key-value store). Multi-file skills must be baked into an image.

Expected syntax when wired:

```yaml
skills:
  my-runbook: |
    ---
    name: my-runbook
    description: Runbook for the Foo service.
    triggers:
      - foo
      - incident
    ---

    # Foo Service Runbook

    Steps to diagnose and remediate common Foo service alerts...
```

**Why not mount the ConfigMap directly at the convergence directory?** A Kubernetes volume
mount replaces the target directory's contents — mounting at `/usr/local/share/agent-skills/installed`
would hide all baked skills. Instead the ConfigMap will be mounted at a separate staging path
and the entrypoint will copy each file into the convergence directory at pod start. See
ADR-0008 in `docs/adr/` for the full rationale.

---

## End-to-end flow (current — baking and ConfigMap delivery pending)

The allowlist and selection-mode path is fully wired. Steps marked `[pending]` require
additional chart and entrypoint wiring.

```
Helm values
    skillsByPhase: {execute: [tdd], review: [code-review]}
    skillsSelectionMode: "triggers"
    skills: {my-runbook: "..."}   ← [pending: chart template not yet wired]
           │
           ▼
temporal-worker Deployment env:
    AGENT_SKILLS_BY_PHASE={"execute":["tdd"],"review":["code-review"]}
    AGENT_SKILLS_SELECTION_MODE=triggers
           │
           ▼
render_job (k8s_jobs.py) — extracts names for the active phase:
    AGENT_SKILLS_ENABLED=tdd          (or "" for [], absent when key missing)
    AGENT_SKILLS_SELECTION_MODE=triggers
           │
           ▼
Agent Execution Job pod start:
    [pending] install_configmap_skills(staging_path)
      → copies ConfigMap skills into /usr/local/share/agent-skills/installed/
           │
           ▼
run_agent (entrypoint.py):
    resolve_skills(phase="execute", allowlist={"execute": ["tdd"]})
      → loads only the "tdd" skill from the convergence directory
           │
           ▼
build_agent(llm, cli_mode=True, agent_context=AgentContext(skills=[tdd_skill]))
      → OpenHands agent runs with TDD skill injected
```

---

## Troubleshooting

**Skills not appearing in a phase**: confirm `skillsByPhase` does not have the phase set to
`[]`. Check that the skill name in `skillsByPhase` exactly matches the `name:` field in the
skill's `SKILL.md` frontmatter (case-sensitive).

**No skills visible despite baked skills in the image**: confirm `skillsByPhase` does not
include an explicit `[]` for that phase. A missing key means all installed skills are
available; an explicit empty list means zero skills.

**Phase receives no skills at all**: check the entrypoint logs for `"resolved N skill(s) for
phase"`. If `N=0` and you expect skills, the convergence directory
`/usr/local/share/agent-skills/installed` may be empty. Verify with:

```sh
kubectl exec -it <pod> -- ls /usr/local/share/agent-skills/installed/
```

**ConfigMap skill not loading**: confirm the ConfigMap key name matches the `name:` field in
the frontmatter. Check for symlinks if using `npx skills` — commit real files, not symlinks
(see `images/agent-base/SKILLS.md`).
