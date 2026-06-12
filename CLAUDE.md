# Project-specific instructions

See `CONTEXT.md` for the project glossary and `docs/adr/` for architecture decisions.

## Project Registry required-fields sync

The required fields for a Project Registry entry (`.Values.projects[]`) are
declared in two places that must stay in sync:

- `_REQUIRED_FIELDS` in `src/devloop/projects.py`
- the `required` checks in `charts/devloop/templates/projects-configmap.yaml`

If you add, remove, or rename a required field in one, update the other and
the test that asserts they match.
