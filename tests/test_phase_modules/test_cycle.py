"""Unit tests for CICycle — the CI fix loop extracted from DevLoopWorkflow.

CICycle is a plain async class (not a Temporal workflow).  It calls activity
functions through injectable callbacks so unit tests can exercise every code
path without spinning up a WorkflowEnvironment.
"""

from __future__ import annotations

import pytest

from devloop.phases.cycle import CICycle, _Callbacks
from devloop.shared import CICheckFailure, CIChecksResult


class _MockState:
    """Mutable mock state for the callback-driven CICycle test harness."""

    def __init__(self) -> None:
        self.ci_poll_results: list[CIChecksResult] = []
        self.ci_poll_index: int = 0
        self.dispatch_commits: list[int] = [1]
        self.comments: list[tuple[int, str]] = []  # (issue_no, body)
        self.kpi_bumps: list[tuple[str, int]] = []
        self.cleanup_names: list[str] = []
        self.dispatch_count: int = 0


@pytest.fixture
def state() -> _MockState:
    return _MockState()


def _make_callbacks(state: _MockState) -> _Callbacks:
    """Build the _Callbacks object for the CICycle under test."""

    async def _poll_ci(project_id: str, pr_number: int) -> CIChecksResult:
        idx = min(state.ci_poll_index, len(state.ci_poll_results) - 1)
        state.ci_poll_index += 1
        return state.ci_poll_results[idx]

    async def _dispatch_fix(project_id, issue_no, spec_dict, poll_interval=5.0):
        state.dispatch_count += 1
        attempt = state.dispatch_count
        commits_seq = state.dispatch_commits or [1]
        commits = commits_seq[min(attempt - 1, len(commits_seq) - 1)]
        if commits > 0:
            state.cleanup_names.append(f"cf{issue_no}")
        return commits

    async def _post_comment(project_id, issue_number, body):
        state.comments.append((issue_number, body))

    async def _kpi_bump(key, n=1):
        state.kpi_bumps.append((key, n))

    async def _cleanup(name):
        if name:
            state.cleanup_names.append(name)

    return _Callbacks(
        poll_ci=_poll_ci,
        dispatch_fix=_dispatch_fix,
        post_comment=_post_comment,
        kpi_bump=_kpi_bump,
        cleanup=_cleanup,
    )


class TestCICycleBasic:
    """CICycle exits immediately when CI is already green."""

    async def test_ci_already_passing(self, state: _MockState) -> None:
        """When CI is already green, CICycle does nothing and returns exhausted=False."""
        state.ci_poll_results = [CIChecksResult(all_passed=True, failures=[])]
        state.dispatch_commits = [1]

        result = await CICycle().run(
            project_id="omneval",
            issue_no=1,
            exec_result={
                "branch": "agent/issue-1",
                "pr_url": "https://github.com/omneval/omneval/pull/1",
            },
            ci_fix_max_iterations=5,
            poll_interval_seconds=5.0,
            callbacks=_make_callbacks(state),
        )
        assert result.exhausted is False
        assert result.commits == 0


class TestCICycleSingleFix:
    """CICycle dispatches one fix pass when CI is red and fix succeeds."""

    async def test_fix_resolves_ci(self, state: _MockState) -> None:
        """One fix pass is dispatched; after the fix CI turns green."""
        state.ci_poll_results = [
            CIChecksResult(
                all_passed=False,
                failures=[CICheckFailure(name="pytest", conclusion="failure")],
            ),
            CIChecksResult(all_passed=True, failures=[]),
        ]
        state.dispatch_commits = [2]

        result = await CICycle().run(
            project_id="omneval",
            issue_no=1,
            exec_result={
                "branch": "agent/issue-1",
                "pr_url": "https://github.com/omneval/omneval/pull/1",
            },
            ci_fix_max_iterations=5,
            poll_interval_seconds=5.0,
            callbacks=_make_callbacks(state),
        )
        assert result.exhausted is False
        assert result.commits == 2
        assert state.dispatch_count == 1
        # Verify queued comment was posted
        queued_comments = [
            c
            for _, c in state.comments
            if "queued" in c.lower() and "ci fix" in c.lower()
        ]
        assert len(queued_comments) == 1
        assert "1/5" in queued_comments[0]
        # Verify result comment was posted
        result_comments = [c for _, c in state.comments if "🔧" in c]
        assert len(result_comments) == 1
        assert "pushed 2 commit" in result_comments[0]


class TestCICycleExhaustion:
    """CICycle exhausts all fix attempts when CI never turns green."""

    async def test_exhausted_after_max_attempts(self, state: _MockState) -> None:
        """CI never goes green; CICycle runs fix_attempts times and returns exhausted=True."""
        state.ci_poll_results = [
            CIChecksResult(
                all_passed=False,
                failures=[CICheckFailure(name="pytest", conclusion="failure")],
            ),
        ]
        state.dispatch_commits = [1]

        result = await CICycle().run(
            project_id="omneval",
            issue_no=1,
            exec_result={
                "branch": "agent/issue-1",
                "pr_url": "https://github.com/omneval/omneval/pull/1",
            },
            ci_fix_max_iterations=3,
            poll_interval_seconds=5.0,
            callbacks=_make_callbacks(state),
        )
        assert result.exhausted is True
        assert result.commits == 3  # 1 per dispatch * 3
        assert state.dispatch_count == 3


class TestCICycleNoPR:
    """CICycle returns immediately when there is no PR URL."""

    async def test_no_pr_url_returns_early(self, state: _MockState) -> None:
        """Without a PR URL, CICycle short-circuits with exhausted=False."""
        state.ci_poll_results = []  # should never be called

        result = await CICycle().run(
            project_id="omneval",
            issue_no=1,
            exec_result={
                "branch": "agent/issue-1",
                # No pr_url
            },
            ci_fix_max_iterations=3,
            poll_interval_seconds=5.0,
            callbacks=_make_callbacks(state),
        )
        assert result.exhausted is False
        assert result.commits == 0
        assert state.ci_poll_index == 0  # no polls made
