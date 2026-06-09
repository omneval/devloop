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

# REVIEW PROCESS

1. **Check completeness FIRST**: Go through every requirement and acceptance criterion in the issue above, one by one, and verify the diff actually implements it. Criteria spanning other languages or layers (UI, SDKs, docs) count — an unimplemented criterion is a finding, not an acceptable scope reduction. A claim in the PR description or a code comment is not an implementation.

2. **Understand the change**: Read the diff to understand the intent.

3. **Check correctness**:
   - Does the implementation match the issue's specified behaviour, API shapes, and response formats exactly? (Field names, pagination envelopes, attribute fallbacks — verify against the issue text, not from memory.)
   - Are edge cases handled (NULL vs empty string, nil vs empty collections, missing data)?
   - Are new/changed behaviours covered by tests?
   - Are errors handled rather than swallowed?
   - Does the change introduce injection vulnerabilities, credential leaks, or other security issues?

4. **Analyse for improvements**: Look for opportunities to:
   - Reduce unnecessary complexity and nesting
   - Eliminate redundant code and abstractions
   - Improve readability through clear variable and function names (prefer verbose over terse)
   - Consolidate related logic
   - Choose clarity over brevity — explicit code is often better than overly compact code

5. **Apply project standards**: If the repo documents coding standards (e.g. a CODING_STANDARDS.md or CONTRIBUTING.md), read and note any deviations.

# REPORT YOUR FINDINGS

Summarise your review so it can be posted to the pull request. Emit a single
`<review>` block containing JSON with a plain-English `summary`, optional
`inline_comments` anchoring specific notes to a file and line, and a `verdict`
that determines the next workflow step:

```
<review>
{
  "summary": "Backend and SDK criteria are implemented, but two acceptance criteria are unmet: (1) the Conversations UI tab, (2) keyset pagination on GET /conversations. Also: nil slice serialises to null instead of [].",
  "verdict": "needs_fixes",
  "inline_comments": [
    {"file": "src/foo.py", "line": 42, "body": "This branch is unreachable — `n` is always > 0 here."}
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
