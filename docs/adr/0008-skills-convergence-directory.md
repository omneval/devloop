# ADR-0008: Skills convergence directory with stage-and-install for ConfigMap delivery

**Status**: Accepted
**Date**: 2026-06-03
**Issues**: #34 (ConfigMap delivery end-to-end)

---

## Context

Agent Skills can reach a running pod by two paths:

1. **Baked into the image**: committed as real `SKILL.md` files under `images/agent-base/skills/`
   and `COPY`-ed into the image at build time.
2. **Delivered at deploy time**: supplied as Kubernetes ConfigMap keys, each value being a
   `SKILL.md` document, mounted into the pod without a rebuild.

Both paths must coexist in the same pod: a per-project image bakes domain-specific skills;
the Helm operator supplies global or override skills via a ConfigMap.

The naive implementation for ConfigMap delivery is to mount the ConfigMap volume directly at
the installed-skills path (`/usr/local/share/agent-skills/installed`). This is wrong: a
Kubernetes volume mount replaces the target directory's contents, hiding every skill baked
into the image. This is the entire problem this ADR addresses.

Alternatives considered:

1. **Mount the ConfigMap directly at the convergence directory** — rejected: hides baked skills.
2. **`subPath` mounts for each ConfigMap key** — preserves baked skills but requires enumerating
   every skill name in the pod spec, defeating the operator's ability to add a skill by adding a
   ConfigMap key.
3. **`initContainer` to merge files** — adds manifest complexity, requires an init image with
   shell utilities, and introduces an additional failure domain. Deferred.
4. **Stage-and-install (chosen)**: mount the ConfigMap at a read-only staging path; the
   entrypoint calls `install_configmap_skills(staging_path)` at pod startup to copy each file
   into the convergence directory. Baked and ConfigMap skills coexist.

---

## Decision

Every Agent Skill resolves through a single **Skills convergence directory**:

```
/usr/local/share/agent-skills/installed   (default, overridable via AGENT_SKILLS_DIR)
```

Skills baked into the image sit here directly (via `COPY` in the Dockerfile). Skills delivered
via a Helm-managed ConfigMap are mounted at a separate read-only staging path
(`/tmp/agent-skills-staging`) and installed into the convergence directory by `main()` at pod
start via `skills.install_configmap_skills(staging_path)`. ConfigMap-delivered skills win on
name collision — they overwrite a same-named baked skill.

The agent loads the merged set once via `load_installed_skills(installed_dir)` in `run_agent`.

---

## Implementation status

`install_configmap_skills(staging_path)` in `images/agent-base/skills.py` is implemented:

- Reads each file from the staging directory (one file per skill, named by skill name).
- Creates `<convergence>/<skill_name>/SKILL.md` for each, overwriting if it exists.
- Returns the list of successfully installed skill names.
- Failures are logged as warnings and do not abort the phase (best-effort).

Two pieces are **not yet wired**:

1. The Helm chart `skills:` value and `agent-skills-configmap.yaml` template (the operator
   interface for delivering ConfigMap skills).
2. The `main()` call to `install_configmap_skills` at pod startup in `entrypoint.py`.

Until both land, only baked skills are loaded. The convergence directory must be writable by
the agent process; in the Agent Base Image it is created at image build time with appropriate
ownership.

---

## Constraints

**ConfigMap-delivered skills are single-file only**: a Kubernetes ConfigMap is a flat
`key → string` map and cannot express `scripts/`, `assets/`, or `references/` subdirectories.
The 1 MiB total ConfigMap size limit also applies. Skills that need multi-file structure must
be baked into an image.

**Symlinks do not survive `COPY`**: Docker's `COPY` instruction does not follow symlinks.
Skills vendored with `npx skills@latest add` must be committed as real `SKILL.md` files, not
symlinks, before the `COPY` step in the Dockerfile picks them up.

---

## Consequences

- Once wired: the convergence directory path (`/usr/local/share/agent-skills/installed`) and
  the staging path are shared contracts between the Helm chart, `k8s_jobs` `render_job`, and
  the agent-base entrypoint. Changing either path touches all three.
- `AGENT_SKILLS_DIR` overrides the convergence directory in tests and local development
  (fully operative today).
- The Helm chart must not mount any volume directly at the convergence directory — only at the
  staging path. Violating this hides all baked skills (the problem this ADR exists to prevent).
