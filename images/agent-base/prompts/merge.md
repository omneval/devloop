# TASK

Merge the following branches into the current branch:

{{BRANCHES}}

For each branch:

1. Run `git merge <branch> --no-edit`
2. If there are merge conflicts, resolve them intelligently by reading both sides and choosing the correct resolution
3. After resolving conflicts, build, lint, and run the project's test suite to verify everything is clean
4. If any of those fail, fix the issues before proceeding to the next branch

After all branches are merged, make a single commit summarizing the merge.

# CLOSE ISSUES

For each branch that was merged, close its issue using the following command:

`gh issue close <ID> --comment "Completed by Homelab Agent"`

Here are all the issues:

{{ISSUES}}

Once you've merged everything you can, YOU MUST output exactly <promise>COMPLETE</promise>.
