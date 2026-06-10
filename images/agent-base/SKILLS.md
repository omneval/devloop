# Agent Skills — Developer Guide

This guide covers how to vendor, bake, test, and override Agent Skills in the
`devloop-agent-base` image and in per-project agent images that extend it.

---

## What is a skill?

A skill is a `SKILL.md` file — a model-agnostic, reusable capability definition. The format
is:

```
---
name: my-skill
description: One-line description the agent sees in <available_skills>.
triggers:
  - keyword-one
  - keyword-two
---

# Full skill content

The agent reads this body on demand via invoke_skill() — costs no context until used.
```

Skills extend the agent's capabilities at runtime. Each skill is loaded into `AgentContext`
before the conversation starts. The agent uses native progressive disclosure: the name and
description appear in `<available_skills>`; the full body is only fetched when the agent
invokes the skill.

---

## Adding skills to the agent image

### 1. Vendor a skill with `npx skills`

Run this from the repo root (or `images/agent-base/`):

```sh
npx skills@latest add <source>
# Example:
npx skills@latest add mattpocock/skills
```

This writes one or more skill directories under `images/agent-base/skills/<skill-name>/`.

### 2. Commit real `SKILL.md` files — not symlinks

`npx skills` may create symlinks in the local `.claude/skills/` directory. **Do not commit
symlinks.** Docker's `COPY` instruction does not follow symlinks; a symlinked `SKILL.md` would
silently produce a broken path in the image.

Verify you are committing real files:

```sh
ls -la images/agent-base/skills/<skill-name>/SKILL.md
# Should show a regular file (-rw-r--r--), not a symlink (lrwxr-xr-x)
```

### 3. Add the `COPY` step to the Dockerfile

Baking skills into the image requires a `COPY` step in the Dockerfile (not yet present in
this branch — tracked separately):

```dockerfile
# After the entrypoint COPY:
COPY skills/ /usr/local/share/agent-skills/installed/
```

Once added, verify the skill landed:

```sh
docker run --rm ghcr.io/omneval/devloop-agent-base:latest \
  ls /usr/local/share/agent-skills/installed/
```

---

## Directory layout

```
images/agent-base/
  skills/                    # vendor skills here (Dockerfile COPY pending)
    <skill-name>/
      SKILL.md               # vendored skill definition; commit this real file
  skills.py                  # resolve_skills, install_configmap_skills, format_skipped_notice
  entrypoint.py              # build_agent seam; calls resolve_skills before running the agent
```

The on-image convergence directory is `/usr/local/share/agent-skills/installed/`.

---

## Testing skills locally

Point `AGENT_SKILLS_DIR` at your local skills directory before running the entrypoint:

```sh
AGENT_SKILLS_DIR=images/agent-base/skills python images/agent-base/entrypoint.py
```

In pytest, use `monkeypatch.setenv`:

```python
def test_my_skill_loads(monkeypatch, tmp_path):
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: my-skill\ndescription: test\n---\n")
    monkeypatch.setenv("AGENT_SKILLS_DIR", str(tmp_path))
    # ... call resolve_skills and assert
```

`resolve_skills` also accepts a `_loader` injection point so tests can avoid touching the
filesystem entirely — see `images/agent-base/test_skills.py` for examples.

---

## Per-project override (extending the base image)

To add project-specific skills in a derived image, `COPY` them on top of the base:

```dockerfile
FROM ghcr.io/omneval/devloop-agent-base:latest

# Project-specific skills layered on top of the base image's baked skills.
COPY skills/ /usr/local/share/agent-skills/installed/
```

Each `COPY` layer adds to the convergence directory without removing prior entries. A
same-named skill in the derived layer overwrites the base image's version.

---

## Delivery channels and precedence

Skills reach the convergence directory through three channels; on a name
collision the most-specific channel wins:

1. **Baked** (image `COPY` at build time) — generic skills shared by every
   project; the agent-base bundles `tdd`, `improve-codebase-architecture`,
   and `to-issues`.
2. **ConfigMap** (`install_configmap_skills`, called by `main()` at pod start
   when `AGENT_SKILLS_CONFIGMAP` is set) — operator-supplied, deploy-time
   skills via the Helm `skills:` value. **Single-file only**: a ConfigMap
   cannot express `scripts/`, `assets/`, or `references/` subdirectories and
   has a 1 MiB total size limit.
3. **Repo-native** (`install_repo_skills`, called by `run_agent` after the
   clone) — `.devloop/skills/<name>/` directories in the enrolled repo, full
   AgentSkills trees included. The channel for project-specific and
   multi-file skills; versions with the code it serves.

See ADR-0008 and `docs/operator-skills.md` for the design rationale.

---

## The `build_agent` override seam (ADR-0007)

`build_agent(llm, cli_mode, agent_context)` in `entrypoint.py` constructs the OpenHands
`Agent`. It replicates the upstream `get_default_agent` preset (terminal + file_editor +
task_tracker tools, LLM-summarising condenser) and adds `agent_context` to inject installed
skills.

A derived image can replace `build_agent` to add custom tools or change the condenser:

```python
# In a derived entrypoint that imports the base:
import entrypoint as _base

def build_agent(llm, cli_mode=True, agent_context=None):
    from openhands.sdk import Agent
    from openhands.tools.preset.default import get_default_condenser, get_default_tools
    from myorg.tools import MyCustomTool

    tools = get_default_tools(enable_browser=not cli_mode) + [MyCustomTool()]
    condenser = get_default_condenser(llm=llm.model_copy(update={"usage_id": "condenser"}))
    return Agent(
        llm=llm,
        tools=tools,
        system_prompt_kwargs={"cli_mode": cli_mode},
        condenser=condenser,
        agent_context=agent_context,
    )

_base.build_agent = build_agent
```

`agent_context=None` is the no-op path — the agent is built identically to the pre-skills
baseline, so this seam has no cost when no skills are loaded.

**Do not call `get_default_agent` directly**: it has no `agent_context` parameter. The
hand-rolled construction is intentional; see ADR-0007 in `docs/adr/`.

---

## Skill selection modes

The `skillsSelectionMode` Helm value (forwarded as `AGENT_SKILLS_SELECTION_MODE`) controls
how eligible skills are presented to the agent:

| Mode | Behaviour |
|------|-----------|
| `"triggers"` (default) | A skill surfaces only when the conversation context matches the skill's `triggers:` frontmatter keywords. Lowest context noise. |
| `"advanced"` | All phase-eligible skills are surfaced to the model; the model selects the most appropriate one autonomously. Use when trigger matching is too narrow. |

---

## Troubleshooting

**Skills not loading**: confirm each skill directory under
`/usr/local/share/agent-skills/installed/` contains a real `SKILL.md` (not a symlink). The
entrypoint logs `"resolved N skill(s) for phase"` at `INFO` level and emits OTLP
`skills.loaded` / `skills.skipped` attributes on the `skills.load` span.

**Phase receives fewer skills than expected**: check `skillsByPhase` in Helm values. A phase
key set to `[]` explicitly allows no skills. A name list must exactly match the `name:` field
in each `SKILL.md` frontmatter.

**Symlinks in the image**: run `find /usr/local/share/agent-skills/ -type l` inside the pod.
Any output means symlinks were `COPY`-ed — the `SKILL.md` content was not included. Re-vendor
using the steps above and commit the real files.
