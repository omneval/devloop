# TASK

Fix issue {{TASK_ID}}: {{ISSUE_TITLE}}

Work on branch {{BRANCH}}. Make commits and run tests.

Only work on the issue specified.

{{FEEDBACK}}

# THE ISSUE (FULL TEXT)

{{ISSUE_BODY}}

If the text above is empty, pull the issue with `gh issue view {{TASK_ID}}`. If it references a parent PRD, pull that in too.

# SCOPE

Every requirement and acceptance criterion in the issue is in scope, regardless of layer — Python package, agent images, Helm chart, docs. You are NOT allowed to declare any part of the issue "out of scope" or "follow-up work". The only acceptable reason to leave a criterion unimplemented is a hard blocker you cannot resolve, and then you must say so explicitly via a `QUESTION:` line or in your final summary.

First action: write the issue's acceptance criteria into a checklist using the task tracker, one entry per criterion. Work through them all.

# CONTEXT

Review recent history to understand current conventions:

```
git log -n 10 --format="%H%n%ad%n%B---" --date=short
```

devloop is a Python project managed with [uv](https://github.com/astral-sh/uv): the `omneval-devloop` package lives under `src/devloop/`, with tests in `tests/` and per-image code in `images/`. It also ships a Helm chart under `charts/devloop/`. Read `CONTEXT.md` at the repo root for the domain language (use these terms in names and tests), `docs/adr/` for architecture decisions, and `.devloop/CODING_STANDARDS.md` for style. Honour the conventions: `uv` only (never `requirements.txt`), version floors not exact pins, and the single-owner `devloop.shared` ConfigMap contract.

# EXPLORATION

Explore the repo and fill your context window with relevant information that will allow you to complete the task.

Pay extra attention to test files that touch the relevant parts of the code (`tests/` for the package, `images/agent-base/test_*.py` for the agent runtime, `charts/devloop/tests/` for the chart).

# EXECUTION

This issue involves writing code, so drive it test-first. Invoke the `tdd` skill (`invoke_skill('tdd')`) and follow its red-green-refactor loop — write ONE failing test, then the minimum code to pass it, then repeat. Never write all the tests up front (the skill calls this the horizontal-slicing anti-pattern).

Use the right feedback tool for the layer you are changing:

- **Python** (`src/devloop/`, `images/`): `uv run pytest` to confirm each RED test fails and each GREEN step passes. For a focused loop, target the test: `uv run pytest tests/test_foo.py::test_bar`.
- **Helm chart** (`charts/devloop/`): `helm unittest charts/devloop`. Add or update a test under `charts/devloop/tests/` for any template behaviour change.

1. RED: write one failing test and confirm it fails
2. GREEN: write the minimum implementation to make that test pass
3. REPEAT until all acceptance criteria are covered
4. REFACTOR: clean up without breaking tests

If you genuinely cannot proceed without a human decision, emit a single line starting with `QUESTION:` followed by the question, then stop and wait.

# FEEDBACK LOOPS

Before committing, run the checks for every layer you touched and confirm they all pass with no errors:

```
# Python changes:
uv sync --all-groups
uv run ruff check src/ tests/
uv run ty check src/
uv run pytest

# Helm chart changes:
helm unittest charts/devloop
helm lint charts/devloop
```

# LINT

Before making any commits, the code must pass all lint checks. 

1. Use `uv run ruff format .` to first format the codebase
2. Use `uv run ruff check --fix .` and examine the output for any issues and fix them.
3. Repeat steps 1-2 until step 2 yields no issues.

# COMMIT

Make a git commit. Use a Conventional Commit subject (`feat:`, `fix:`, `chore:`, `docs:`, `test:`) — this matches the repo's history and the release tooling. The message must:

1. Reference the issue (e.g. `fixes #{{TASK_ID}}`)
2. Summarize the change and any key decisions
3. Note blockers or follow-ups for the next iteration

Keep it concise.

# BEFORE YOU DECLARE COMPLETE

Re-read the acceptance criteria in the issue text above, one by one. For each, verify the implementation exists in your diff (`git diff main...HEAD`) — not in a comment, not in a plan, in working code with tests. If any criterion is unmet, go back to EXECUTION.

Then end your final message with a checklist of every acceptance criterion marked ✅ implemented or ❌ not implemented (with the reason).

If the task is not complete, leave a comment on the issue with what was done and what remains. Do not close the issue — this will be done by the merge agent.

Once complete, YOU MUST output exactly <promise>COMPLETE</promise>.

# FINAL RULES

ONLY WORK ON A SINGLE TASK.
