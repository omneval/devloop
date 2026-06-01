# TASK

Fix issue {{TASK_ID}}: {{ISSUE_TITLE}}

Pull in the issue using `gh issue view {{TASK_ID}}`. If it has a parent PRD, pull that in too.

Only work on the issue specified.

Work on branch {{BRANCH}}. Make commits and run tests.

# CONTEXT

Review recent history to understand current conventions:

```
git log -n 10 --format="%H%n%ad%n%B---" --date=short
```

# EXPLORATION

Explore the repo and fill your context window with relevant information that will allow you to complete the task.

Identify the project's language and tooling from the files present (e.g. go.mod, pyproject.toml, package.json) and use that ecosystem's standard build/lint/test commands throughout.

Pay extra attention to test files that touch the relevant parts of the code.

# EXECUTION

Use a red-green-refactor (TDD) loop to complete the task:

1. RED: write one failing test and confirm it fails by running the project's test suite
2. GREEN: write the minimum implementation to make that test pass
3. REPEAT until all acceptance criteria are covered
4. REFACTOR: clean up without breaking tests

If you genuinely cannot proceed without a human decision, emit a single line starting with `QUESTION:` followed by the question, then stop and wait.

# FEEDBACK LOOPS

Before committing, build, lint, and test the project using its standard tooling. All of them must pass with no errors before you commit.

# COMMIT

Make a git commit. The commit message must:

1. Reference the issue (e.g. `fixes #{{TASK_ID}}`)
2. Summarize the change and any key decisions
3. Note blockers or follow-ups for the next iteration

Keep it concise.

# THE ISSUE

If the task is not complete, leave a comment on the issue with what was done and what remains.

Do not close the issue — this will be done by the merge agent.

Once complete, YOU MUST output exactly <promise>COMPLETE</promise>.

# FINAL RULES

ONLY WORK ON A SINGLE TASK.
