# TASK

Review the code changes on branch `{{BRANCH}}`, which implement issue #{{ISSUE_NUMBER}}. This is a **comment-only** review — do not edit any files. Analyse the diff, post findings, and return a structured verdict.

# THE ISSUE BEING IMPLEMENTED (FULL TEXT)

{{ISSUE_BODY}}

If the text above is empty, pull the issue with `gh issue view {{ISSUE_NUMBER}}`.

# CONTEXT

Read the change you are reviewing:

```
git diff {{SOURCE_BRANCH}}...{{BRANCH}}
git log {{SOURCE_BRANCH}}..{{BRANCH}} --oneline
```

devloop is a Python project managed with uv: the `omneval-devloop` package lives under `src/devloop/`, tests in `tests/`, per-image code in `images/`, and a Helm chart under `charts/devloop/`. Read `CONTEXT.md` for the domain language and `docs/adr/` for architecture decisions.

# REVIEW PROCESS

1. **Check completeness FIRST**: Go through every requirement and acceptance criterion in the issue above, one by one, and verify the diff actually implements it. Criteria in any layer count (Python package, agent images, Helm chart, docs) — an unimplemented criterion is a finding, not an acceptable scope reduction. A claim in the PR description or a code comment is not an implementation.

2. **Understand the change**: Read the diff to understand the intent.

3. **Check correctness**:
   - Does the implementation match the issue's specified behaviour exactly?
   - Are edge cases handled?
   - Are new/changed behaviours covered by tests (`tests/`, `images/*/test_*.py`, or `charts/devloop/tests/`)?
   - Does it follow the repo conventions: `uv` only (no `requirements.txt`), version floors not `==` pins, modern typing (`list[str]`, `X | None`), `from __future__ import annotations`?
   - Is the `devloop.shared` ConfigMap contract (`result` / `human_answer` keys, `AgentJobResult`) referenced from its single owner rather than duplicated?
   - Does the change contradict an ADR in `docs/adr/`? If so, flag it rather than silently diverging.
   - Does the change introduce injection vulnerabilities, credential leaks, or other security issues?

4. **Apply project standards**: Follow `.devloop/CODING_STANDARDS.md` and the conventions in `CONTEXT.md`.

# REPORT YOUR FINDINGS

Summarise your review so it can be posted to the pull request. Emit a single `<review>` block containing JSON with a plain-English `summary`, optional `inline_comments` anchoring specific notes to a file and line, and a `verdict` that determines the next workflow step:

```
<review>
{
  "summary": "The activity and workflow changes are correct and tested, but one acceptance criterion is unmet: the Helm chart exposes no value for the new env var.",
  "verdict": "needs_fixes",
  "inline_comments": [
    {"file": "src/devloop/dev_loop.py", "line": 42, "body": "This branch is unreachable — commits is always > 0 here."}
  ]
}
</review>
```

## Verdict values

- **lgtm** — Every acceptance criterion is implemented and the change is ready to merge. No further work needed.
- **needs_fixes** — There are actionable issues that can be resolved automatically: unmet acceptance criteria, bugs, or missing tests. Enumerate each one explicitly in the summary so the Fix Pass agent knows exactly what to address.
- **needs_human** — The change requires human judgement (ambiguous intent, architectural decisions, or security concerns). The issue will be parked.

An unmet acceptance criterion always means at least **needs_fixes** — never lgtm.

`inline_comments` may be an empty list. Always include a `verdict`.

Once complete, YOU MUST output exactly <promise>COMPLETE</promise>.
