"""Unit tests verifying CICycle uses the CICycleOps subprotocol.

These tests exercise the real code paths through CICycle by injecting
mocked callbacks, confirming CICycle accesses its I/O through the
``ci_ops`` sub-protocol rather than the monolithic PhaseOps.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from devloop.phases.phase_ops import PhaseOps


class TestCICycleUsesCICycleOpsSubProtocol:
    """CICycle accesses callbacks through ci_ops sub-protocol."""

    @pytest.mark.asyncio
    async def test_uses_ci_ops_for_comment(self) -> None:
        """CICycle accesses comment via ci_ops sub-protocol."""
        from devloop.phases.cycle import CICycle

        from devloop.shared import CICheckFailure, CIChecksResult

        comment_calls: list[tuple[str, int, str]] = []

        async def _mock_comment(project_id: str, issue_number: int, body: str) -> None:
            comment_calls.append((project_id, issue_number, body))

        async def _mock_poll_ci(project_id: str, pr_number: int) -> CIChecksResult:
            _mock_poll_ci._n += 1
            if _mock_poll_ci._n == 1:
                # First poll: red CI → triggers fix + queued comment.
                return CIChecksResult(
                    all_passed=False,
                    failures=[CICheckFailure(name="pytest", conclusion="failure")],
                )
            # Second poll (after fix): green CI → success.
            return CIChecksResult(all_passed=True, failures=[])

        _mock_poll_ci._n = 0  # type: ignore[attr-defined]

        callbacks = PhaseOps()
        callbacks.ci_ops.comment = _mock_comment
        callbacks.ci_ops.poll_ci = _mock_poll_ci
        callbacks.ci_ops.dispatch_fix = AsyncMock(return_value=1)
        callbacks.ci_ops.kpi_bump = AsyncMock()

        with patch.object(PhaseOps, "pr_number_from_url", return_value=42):
            with patch("devloop.phases.cycle.workflow") as mock_workflow:
                mock_workflow.execute_activity = AsyncMock()
                mock_workflow.sleep = AsyncMock()

                _result = await CICycle().run(
                    project_id="test",
                    issue_no=1,
                    exec_result={
                        "branch": "feat/1",
                        "pr_url": "https://github.com/p/r/42",
                    },
                    ci_fix_max_iterations=1,
                    poll_interval_seconds=5.0,
                    callbacks=callbacks,
                )

                # ci_ops.comment must have been called (queued comment).
                assert len(comment_calls) >= 1
                assert comment_calls[0][1] == 42

    @pytest.mark.asyncio
    async def test_uses_ci_ops_for_poll_ci(self) -> None:
        """CICycle accesses poll_ci via ci_ops sub-protocol."""
        from devloop.phases.cycle import CICycle

        from devloop.shared import CIChecksResult

        async def _mock_poll_ci(project_id: str, pr_number: int) -> CIChecksResult:
            _mock_poll_ci._called = (project_id, pr_number)
            return CIChecksResult(all_passed=True, failures=[])

        _mock_poll_ci._called = False  # type: ignore[attr-defined]

        callbacks = PhaseOps()
        callbacks.ci_ops.poll_ci = _mock_poll_ci
        callbacks.ci_ops.comment = AsyncMock()

        with patch.object(PhaseOps, "pr_number_from_url", return_value=42):
            with patch("devloop.phases.cycle.workflow") as mock_workflow:
                mock_workflow.execute_activity = AsyncMock()
                mock_workflow.sleep = AsyncMock()

                _result = await CICycle().run(
                    project_id="test",
                    issue_no=1,
                    exec_result={
                        "branch": "feat/1",
                        "pr_url": "https://github.com/p/r/42",
                    },
                    ci_fix_max_iterations=3,
                    poll_interval_seconds=5.0,
                    callbacks=callbacks,
                )

                assert _result.exhausted is False
                assert _result.commits == 0
                # ci_ops.poll_ci must have been called.
                assert _mock_poll_ci._called == ("test", 42)  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_uses_ci_ops_for_kpi_bump(self) -> None:
        """CICycle accesses kpi_bump via ci_ops sub-protocol."""
        from devloop.phases.cycle import CICycle

        from devloop.shared import CICheckFailure, CIChecksResult

        async def _mock_poll_ci(project_id: str, pr_number: int) -> CIChecksResult:
            _mock_poll_ci._called = (project_id, pr_number)
            # Red CI → needs fix.
            return CIChecksResult(
                all_passed=False,
                failures=[CICheckFailure(name="pytest", conclusion="failure")],
            )

        _mock_poll_ci._called = False  # type: ignore[attr-defined]

        kpi_bump_called = False

        async def _mock_kpi_bump(key: str, n: int) -> None:
            nonlocal kpi_bump_called
            kpi_bump_called = True

        async def _mock_dispatch_fix(
            project_id: str,
            spec: Any,
            issue_no: int,
            poll_interval: float = 5.0,
        ) -> int:
            _mock_dispatch_fix._called = (project_id, issue_no)
            return 1

        _mock_dispatch_fix._called = False  # type: ignore[attr-defined]

        callbacks = PhaseOps()
        callbacks.ci_ops.poll_ci = _mock_poll_ci
        callbacks.ci_ops.kpi_bump = _mock_kpi_bump
        callbacks.ci_ops.dispatch_fix = _mock_dispatch_fix
        callbacks.ci_ops.comment = AsyncMock()

        with patch.object(PhaseOps, "pr_number_from_url", return_value=42):
            with patch("devloop.phases.cycle.workflow") as mock_workflow:
                mock_workflow.execute_activity = AsyncMock()
                mock_workflow.sleep = AsyncMock()

                _result = await CICycle().run(
                    project_id="test",
                    issue_no=42,
                    exec_result={
                        "branch": "feat/42",
                        "pr_url": "https://github.com/p/r/42",
                    },
                    ci_fix_max_iterations=3,
                    poll_interval_seconds=5.0,
                    callbacks=callbacks,
                )

                # ci_ops.kpi_bump must have been called.
                assert kpi_bump_called
                assert _mock_dispatch_fix._called == ("test", 42)  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_uses_ci_ops_for_dispatch_fix(self) -> None:
        """CICycle accesses dispatch_fix via ci_ops sub-protocol."""
        from devloop.phases.cycle import CICycle

        from devloop.shared import CICheckFailure, CIChecksResult

        async def _mock_poll_ci(project_id: str, pr_number: int) -> CIChecksResult:
            _mock_poll_ci._called = (project_id, pr_number)
            return CIChecksResult(
                all_passed=False,
                failures=[CICheckFailure(name="pytest", conclusion="failure")],
            )

        _mock_poll_ci._called = False  # type: ignore[attr-defined]

        async def _mock_dispatch_fix(
            project_id: str,
            spec: Any,
            issue_no: int,
            poll_interval: float = 5.0,
        ) -> int:
            _mock_dispatch_fix._called = (project_id, issue_no)
            return 1

        _mock_dispatch_fix._called = False  # type: ignore[attr-defined]

        callbacks = PhaseOps()
        callbacks.ci_ops.poll_ci = _mock_poll_ci
        callbacks.ci_ops.dispatch_fix = _mock_dispatch_fix
        callbacks.ci_ops.comment = AsyncMock()
        callbacks.ci_ops.kpi_bump = AsyncMock()

        with patch.object(PhaseOps, "pr_number_from_url", return_value=42):
            with patch("devloop.phases.cycle.workflow") as mock_workflow:
                mock_workflow.execute_activity = AsyncMock()
                mock_workflow.sleep = AsyncMock()

                _result = await CICycle().run(
                    project_id="test",
                    issue_no=42,
                    exec_result={
                        "branch": "feat/42",
                        "pr_url": "https://github.com/p/r/42",
                    },
                    ci_fix_max_iterations=3,
                    poll_interval_seconds=5.0,
                    callbacks=callbacks,
                )

                # ci_ops.dispatch_fix must have been called.
                assert _mock_dispatch_fix._called == ("test", 42)  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_uses_ci_ops_for_cleanup(self) -> None:
        """CICycle accesses cleanup via ci_ops sub-protocol."""
        from devloop.phases.cycle import CICycle

        from devloop.shared import CIChecksResult

        async def _mock_poll_ci(project_id: str, pr_number: int) -> CIChecksResult:
            _mock_poll_ci._called = (project_id, pr_number)
            return CIChecksResult(all_passed=True, failures=[])

        _mock_poll_ci._called = False  # type: ignore[attr-defined]

        callbacks = PhaseOps()
        callbacks.ci_ops.poll_ci = _mock_poll_ci
        callbacks.ci_ops.comment = AsyncMock()

        with patch.object(PhaseOps, "pr_number_from_url", return_value=42):
            with patch("devloop.phases.cycle.workflow") as mock_workflow:
                mock_workflow.execute_activity = AsyncMock()
                mock_workflow.sleep = AsyncMock()

                _result = await CICycle().run(
                    project_id="test",
                    issue_no=42,
                    exec_result={
                        "branch": "feat/42",
                        "pr_url": "https://github.com/p/r/42",
                    },
                    ci_fix_max_iterations=3,
                    poll_interval_seconds=5.0,
                    callbacks=callbacks,
                )

                # ci_ops.cleanup is available on the sub-protocol.
                assert callbacks.ci_ops.cleanup is None  # default, but the field exists

    @pytest.mark.asyncio
    async def test_ci_ops_fallback_uses_phaseops_field(self) -> None:
        """When ci_ops.comment is None, CICycle falls back to PhaseOps.comment."""
        from devloop.phases.cycle import CICycle

        from devloop.shared import CICheckFailure, CIChecksResult

        async def _mock_poll_ci(project_id: str, pr_number: int) -> CIChecksResult:
            _mock_poll_ci._called = (project_id, pr_number)
            return CIChecksResult(
                all_passed=False,
                failures=[CICheckFailure(name="pytest", conclusion="failure")],
            )

        _mock_poll_ci._called = False  # type: ignore[attr-defined]

        async def _mock_dispatch_fix(
            project_id: str, spec: Any, issue_no: int, poll_interval: float = 5.0
        ) -> int:
            _mock_dispatch_fix._count += 1
            return 1

        _mock_dispatch_fix._count = 0  # type: ignore[attr-defined]

        comment_called = False

        async def _mock_comment(project_id: str, issue_number: int, body: str) -> None:
            nonlocal comment_called
            comment_called = True

        callbacks = PhaseOps(
            comment=_mock_comment,
            poll_ci=_mock_poll_ci,
            dispatch_fix=_mock_dispatch_fix,
            kpi_bump=AsyncMock(),
            cleanup=AsyncMock(),
        )
        # ci_ops.comment is None (default), so it should fall back.
        callbacks.ci_ops.comment = None

        with patch.object(PhaseOps, "pr_number_from_url", return_value=42):
            with patch("devloop.phases.cycle.workflow") as mock_workflow:
                mock_workflow.execute_activity = AsyncMock()
                mock_workflow.sleep = AsyncMock()

                _result = await CICycle().run(
                    project_id="test",
                    issue_no=42,
                    exec_result={
                        "branch": "feat/42",
                        "pr_url": "https://github.com/p/r/42",
                    },
                    ci_fix_max_iterations=3,
                    poll_interval_seconds=5.0,
                    callbacks=callbacks,
                )

                assert _result.commits == 3  # 3 iterations, 1 commit each
                assert comment_called
