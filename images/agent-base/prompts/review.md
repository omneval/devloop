# TASK

Review the code changes on branch `{{BRANCH}}`. This is a **comment-only** review — do
not edit any files. Analyse the diff, post findings, and return a structured verdict.

# CONTEXT

Read the change you are reviewing:

```
git diff {{SOURCE_BRANCH}}...{{BRANCH}}
git log {{SOURCE_BRANCH}}..{{BRANCH}} --oneline
```

# REVIEW PROCESS

1. **Understand the change**: Read the diff to understand the intent.

2. **Analyse for improvements**: Look for opportunities to:
   - Reduce unnecessary complexity and nesting
   - Eliminate redundant code and abstractions
   - Improve readability through clear variable and function names (prefer verbose over terse)
   - Consolidate related logic
   - Remove unnecessary comments that describe obvious code
   - Choose clarity over brevity — explicit code is often better than overly compact code

3. **Check correctness**:
   - Does the implementation match the intent? Are edge cases handled?
   - Are new/changed behaviours covered by tests?
   - Are errors handled rather than swallowed?
   - Does the change introduce injection vulnerabilities, credential leaks, or other security issues?

4. **Apply project standards**: If the repo documents coding standards (e.g. a CODING_STANDARDS.md or CONTRIBUTING.md), read and note any deviations.

# REPORT YOUR FINDINGS

Summarise your review so it can be posted to the pull request. Emit a single
`<review>` block containing JSON with a plain-English `summary`, optional
`inline_comments` anchoring specific notes to a file and line, and a `verdict`
that determines the next workflow step:

```
<review>
{
  "summary": "Tightened error handling in the parser; the change otherwise looks correct and is well tested.",
  "verdict": "lgtm",
  "inline_comments": [
    {"file": "src/foo.py", "line": 42, "body": "This branch is unreachable — `n` is always > 0 here."}
  ]
}
</review>
```

## Verdict values

- **lgtm** — The change is ready to merge. No further work needed.
- **needs_fixes** — There are actionable issues that can be resolved automatically.
  List them in the summary so the Fix Pass agent knows what to address.
- **needs_human** — The change requires human judgement (ambiguous intent,
  architectural decisions, or security concerns). The issue will be parked.

`inline_comments` may be an empty list. Always include a `verdict`.

Once complete, YOU MUST output exactly <promise>COMPLETE</promise>.
