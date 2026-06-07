# TASK

Another agent, mid-task on branch `{{BRANCH}}`, paused with a clarifying question
it could not answer on its own. Investigate and return the best-informed answer
so it can resume — there is no human available to ask.

# QUESTION

{{QUESTION}}

# CONTEXT

You have full read/write access to the working branch. Use it to ground your
answer in the actual code and history rather than guessing blind:

```
git log -n 10 --format="%H%n%ad%n%B---" --date=short
git diff {{SOURCE_BRANCH}}...{{BRANCH}}
```

Explore the repository — read the relevant source files, existing conventions,
tests, and documentation — to understand exactly what the paused agent needs to
know to proceed correctly.

# ANSWER

Decide on the single best answer to the question above. Prefer answers that:

1. Match the conventions already established in this codebase
2. Unblock the paused agent with a concrete, actionable decision (not a
   restatement of the question or a list of options)
3. Are grounded in what you found exploring the branch — cite the specific
   file, pattern, or precedent that informed your decision when useful

Do **not** make any commits or change any files — you are only answering the
question, not doing the paused agent's work for it.

Once you have decided, output your answer as the final line of your response,
prefixed with `ANSWER:` — for example:

```
ANSWER: Use library A; it's already a dependency (see pyproject.toml) and the
existing HTTP client in src/devloop/github_client.py follows the same pattern.
```

Then output exactly <promise>COMPLETE</promise>.
