# TASK

Fix issue {{TASK_ID}}: {{ISSUE_TITLE}}

Work on branch {{BRANCH}}. Make commits and run tests.

Only work on the issue specified.

{{FEEDBACK}}

# THE ISSUE (FULL TEXT)

{{ISSUE_BODY}}

If the text above is empty, pull the issue with `gh issue view {{TASK_ID}}`. If it references a parent PRD, pull that in too.

# SCOPE — READ THIS BEFORE WRITING ANY CODE

Every requirement and acceptance criterion in the issue is in scope, **regardless of language or layer**. If the issue lists backend, SDK, and UI work, you implement the backend, the SDKs, AND the UI. This repository may contain multiple ecosystems (e.g. Go services plus a TypeScript frontend plus Python/TS/Go SDKs) — the presence of one primary language does NOT make work in the other languages optional.

You are NOT allowed to declare any part of the issue "out of scope", "follow-up work", or "outside this change". The only acceptable reason to leave a criterion unimplemented is a hard blocker you cannot resolve, and then you must say so explicitly via a `QUESTION:` line or in your final summary.

First action: write the issue's acceptance criteria into a checklist using the task tracker, one entry per criterion. Work through them all.

# CONTEXT

Review recent history to understand current conventions:

```
git log -n 10 --format="%H%n%ad%n%B---" --date=short
```

# EXPLORATION

Explore the repo and fill your context window with relevant information that will allow you to complete the task.

Identify EVERY ecosystem the issue touches from the files present (e.g. go.mod / go.work, pyproject.toml, package.json — including nested ones like `ui/package.json` or `sdk/*/`) and use each ecosystem's standard build/lint/test commands for the parts you change there.

Pay extra attention to test files that touch the relevant parts of the code.

# EXECUTION

Use a red-green-refactor (TDD) loop to complete the task:

1. RED: write one failing test and confirm it fails by running that ecosystem's test suite
2. GREEN: write the minimum implementation to make that test pass
3. REPEAT until all acceptance criteria are covered
4. REFACTOR: clean up without breaking tests

If you genuinely cannot proceed without a human decision, emit a single line starting with `QUESTION:` followed by the question, then stop and wait.

# FEEDBACK LOOPS

Before committing, build, lint, and test every ecosystem you touched using its standard tooling. All of them must pass with no errors before you commit.

# COMMIT

Make a git commit. The commit message must:

1. Reference the issue (e.g. `fixes #{{TASK_ID}}`)
2. Summarize the change and any key decisions
3. Note blockers or follow-ups for the next iteration

Keep it concise.

# BEFORE YOU DECLARE COMPLETE

Re-read the acceptance criteria in the issue text above, one by one. For each, verify the implementation exists in your diff (`git diff` against where you started) — not in a comment, not in a plan, in working code with tests. If any criterion is unmet, go back to EXECUTION.

Then end your final message with a checklist of every acceptance criterion marked ✅ implemented or ❌ not implemented (with the reason).

If the task is not complete, leave a comment on the issue with what was done and what remains. Do not close the issue — this will be done by the merge agent.

Once complete, YOU MUST output exactly <promise>COMPLETE</promise>.

# FINAL RULES

ONLY WORK ON A SINGLE TASK.
