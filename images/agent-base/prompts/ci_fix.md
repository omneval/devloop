# TASK

Make the failing CI checks on branch `{{BRANCH}}` pass — and nothing else.

# CONTEXT

The following CI checks are currently failing on this branch:

{{CI_CHECK_FAILURES}}

Review recent history to understand current conventions:

```
git log -n 10 --format="%H%n%ad%n%B---" --date=short
git diff {{SOURCE_BRANCH}}...{{BRANCH}}
```

# EXPLORATION

Identify the project's language and tooling from the files present (e.g. go.mod, pyproject.toml, package.json) and use that ecosystem's standard build/lint/test commands to reproduce each failure locally before changing anything.

Read the failing check's logs/output (the details URLs above, or by re-running the equivalent command locally) to understand exactly why it's failing.

# EXECUTION

Make the **minimal** change required to turn each failing check green:

1. Reproduce the failure locally using the project's standard build/lint/test commands
2. Make the smallest change that fixes the root cause (a failing test, a lint violation, a build error, …)
3. Re-run the check locally to confirm it now passes
4. Repeat for every failing check listed above

Do **not**:

- Refactor, restyle, or "improve" unrelated code
- Change the intent or scope of the original implementation
- Disable, skip, or weaken tests/checks to make them pass artificially

If a failing check cannot be reproduced or fixed without a human decision (e.g. it depends on external infrastructure, secrets, or a genuine design question), emit a single line starting with `QUESTION:` followed by the question, then stop and wait.

# FEEDBACK LOOPS

Before committing, run the project's full build/lint/test suite using its standard tooling — not just the previously-failing checks — to confirm your fix doesn't break anything else.

# COMMIT

Make a git commit on `{{BRANCH}}`. The commit message must:

1. Describe which CI check(s) you fixed and how
2. Note any check you could not fix and why

Keep it concise. If nothing needed to change (the checks are already passing), make no commit.

Once complete, YOU MUST output exactly <promise>COMPLETE</promise>.

# FINAL RULES

ONLY FIX THE FAILING CI CHECKS. Do not expand scope beyond making CI green.
