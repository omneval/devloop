# TASK

A human reviewer has left feedback on your open PR for branch `{{BRANCH}}`.
Make the **targeted** changes the feedback asks for — and nothing else — then
commit and push.

# FEEDBACK

Source: {{FEEDBACK_SOURCE}} from @{{FEEDBACK_AUTHOR}}

> {{COMMENT_BODY}}

# CONTEXT — what's already in the PR

```diff
{{PR_DIFF}}
```

Review recent history to understand current conventions before changing anything:

```
git log -n 10 --format="%H%n%ad%n%B---" --date=short
git diff {{SOURCE_BRANCH}}...{{BRANCH}}
```

# EXECUTION

1. Read the feedback carefully and identify exactly what change(s) it is asking for
2. Locate the relevant code in the diff/branch above
3. Make the **minimal** change that addresses the feedback — match the existing
   conventions and style of the surrounding code
4. Re-run the project's standard build/lint/test commands to confirm nothing broke

Do **not**:

- Expand scope beyond what the feedback asked for
- Refactor, restyle, or "improve" unrelated code
- Re-litigate the feedback — if it's a clear, actionable request, just do it

If the feedback is a question rather than a change request, or genuinely
ambiguous in a way that materially changes what you'd build, emit a single line
starting with `QUESTION:` followed by the question, then stop and wait.

# COMMIT

Make a git commit on `{{BRANCH}}`. The commit message must describe what
changed and reference the feedback it addresses.

# SUMMARY

Once you have committed (or determined no change was needed), end your
response with a short summary of what you did, **explicitly referencing the
commit SHA** of the commit you made (e.g. "Pushed `abc1234`: renamed the
helper per @reviewer's suggestion."). The workflow uses this summary — not a
GitHub comment you post yourself — to notify the reviewer; do not post a PR
comment from inside this session.

Once complete, YOU MUST output exactly <promise>COMPLETE</promise>.

# FINAL RULES

ONLY ADDRESS THE FEEDBACK ABOVE. Do not expand scope beyond the reviewer's request.
