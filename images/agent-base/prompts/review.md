# TASK

Review the code changes on branch `{{BRANCH}}` and improve code clarity, consistency, and maintainability while preserving exact functionality.

# CONTEXT

Read the change you are reviewing:

```
git diff {{SOURCE_BRANCH}}...{{BRANCH}}
git log {{SOURCE_BRANCH}}..{{BRANCH}} --oneline
```

# REVIEW PROCESS

1. **Understand the change**: Read the diff and commits above to understand the intent.

2. **Analyze for improvements**: Look for opportunities to:
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

4. **Maintain balance**: Avoid over-simplification that could:
   - Reduce code clarity or maintainability
   - Create overly clever solutions that are hard to understand
   - Combine too many concerns into single functions
   - Remove helpful abstractions that improve code organization
   - Make the code harder to debug or extend

5. **Apply project standards**: If the repo documents coding standards (e.g. a CODING_STANDARDS.md or CONTRIBUTING.md), read and follow them.

6. **Preserve functionality**: Never change what the code does — only how it does it.

# EXECUTION

If you find improvements to make:

1. Make the changes directly on this branch
2. Build, lint, and run the project's test suite to confirm nothing is broken
3. Commit describing the refinements

If the code is already clean and well-structured, do nothing.

Once complete, YOU MUST output exactly <promise>COMPLETE</promise>.
