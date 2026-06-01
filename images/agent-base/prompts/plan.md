# ISSUES

List the open issues to plan by running:

```
gh issue list --state open --label {{AGENT_LABEL}} --json number,title,body,labels,comments \
  --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'
```

# TASK

Analyze the open issues and build a dependency graph. For each issue, determine whether it **blocks** or **is blocked by** any other open issue. If an issue is missing from the list above then assume it has already been resolved.

omneval is a Go workspace: each service has its own `go.mod` under `go.work`, with shared types in `internal/`. Treat issues that touch overlapping services/packages as likely to conflict.

An issue B is **blocked by** issue A if:

- B requires code or infrastructure that A introduces
- B and A modify overlapping files or modules, making concurrent work likely to produce merge conflicts
- B's requirements depend on a decision or API shape that A will establish

An issue is **unblocked** if it has zero blocking dependencies on other open issues.

For each unblocked issue, assign a branch name using the format `agent/issue-{id}-{slug}`.

{{FEEDBACK}}

# OUTPUT

Output your plan as a JSON object wrapped in `<plan>` tags:

<plan>
{"issues": [{"id": "42", "title": "Fix auth bug", "branch": "agent/issue-42-fix-auth-bug"}]}
</plan>

Include only unblocked issues, highest priority first. If every issue is blocked, include the single highest-priority candidate (the one with the fewest or weakest dependencies).
