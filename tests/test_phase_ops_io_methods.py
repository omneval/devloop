"""Tests for PhaseOps I/O methods — verify _comment, _dispatch, _cleanup,
_request_reviewer, _emit_kpis provide the unified callback protocol.

These tests verify that PhaseOps has methods for each I/O operation that:
1. Use injectable callback fields first when present
2. Fall back to Temporal activity paths when callbacks are None
3. Are exercised by PRCommentWorkflow code paths (via delegation overrides)
"""

from __future__ import annotations

import pytest

from devloop.phases.phase_ops import PhaseOps


class TestPhaseOpsComment:
    """Tests for PhaseOps._comment method."""

    @pytest.mark.asyncio
    async def test_comment_uses_callback_when_set(self) -> None:
        """When self.comment callback is set, _comment calls it directly."""
        ops = PhaseOps()
        call_log: list = []

        async def mock_comment(project_id: str, issue_number: int, body: str) -> None:
            call_log.append((project_id, issue_number, body))

        ops.comment = mock_comment
        await ops._comment("test-project", 42, "Hello")

        assert call_log == [("test-project", 42, "Hello")]

    @pytest.mark.asyncio
    async def test_comment_falls_back_to_activity_when_no_callback(self) -> None:
        """When no callback, _comment invokes the Temporal activity."""
        ops = PhaseOps()
        # callback is None by default
        assert ops.comment is None

        # With no callback, it should attempt the activity.
        # Since we're not in a Temporal context, this should raise an error
        # (workflow.execute_activity requires a workflow context).
        with pytest.raises(Exception):  # noqa: B017
            await ops._comment("test-project", 42, "Hello")


class TestPhaseOpsCleanup:
    """Tests for PhaseOps._cleanup method."""

    @pytest.mark.asyncio
    async def test_cleanup_uses_callback_when_set(self) -> None:
        """When self.cleanup callback is set, _cleanup calls it directly."""
        ops = PhaseOps()
        call_log: list = []

        async def mock_cleanup(job_name: str) -> None:
            call_log.append(job_name)

        ops.cleanup = mock_cleanup
        await ops._cleanup("my-job-123")

        assert call_log == ["my-job-123"]

    @pytest.mark.asyncio
    async def test_cleanup_noop_on_empty_job_name(self) -> None:
        """Empty job name should be a no-op, even without a callback."""
        ops = PhaseOps()
        await ops._cleanup("")
        # Should not raise

    @pytest.mark.asyncio
    async def test_cleanup_falls_back_to_activity_when_no_callback(self) -> None:
        """When no callback, _cleanup invokes the Temporal activity."""
        ops = PhaseOps()
        assert ops.cleanup is None
        with pytest.raises(Exception):  # noqa: B017
            await ops._cleanup("some-job")


class TestPhaseOpsDispatch:
    """Tests for PhaseOps._dispatch method."""

    @pytest.mark.asyncio
    async def test_dispatch_uses_callback_when_set(self) -> None:
        """When self.dispatch callback is set, _dispatch calls it directly."""
        from devloop.execution import AgentJobResult, TaskSpec
        from devloop.shared import JobStatus

        ops = PhaseOps()
        call_log: list = []

        async def mock_dispatch(
            project_id: str,
            spec: TaskSpec,
            issue_number: int,
            poll_interval_seconds: float,
        ) -> AgentJobResult:
            call_log.append((project_id, spec, issue_number, poll_interval_seconds))
            return AgentJobResult(
                status=JobStatus.COMPLETE.value, job_name="dispatched-job"
            )

        ops.dispatch = mock_dispatch
        spec = TaskSpec(phase="test", project_id="p", issue_number=1)
        result = await ops._dispatch(
            "p", spec, issue_number=1, poll_interval_seconds=5.0
        )

        assert len(call_log) == 1
        assert call_log[0][0] == "p"
        assert call_log[0][2] == 1
        assert result.status == JobStatus.COMPLETE.value

    @pytest.mark.asyncio
    async def test_dispatch_falls_back_to_activity_when_no_callback(self) -> None:
        """When no callback, _dispatch invokes the Temporal activity."""
        from devloop.execution import TaskSpec

        ops = PhaseOps()
        assert ops.dispatch is None
        spec = TaskSpec(phase="test", project_id="p", issue_number=1)
        with pytest.raises(Exception):  # noqa: B017
            await ops._dispatch("p", spec, issue_number=1, poll_interval_seconds=5.0)


class TestPhaseOpsRequestReviewer:
    """Tests for PhaseOps._request_reviewer method."""

    @pytest.mark.asyncio
    async def test_request_reviewer_uses_callback_when_set(self) -> None:
        """When self.request_reviewer callback is set, _request_reviewer calls it directly."""
        from devloop.github import ReviewerRequestResult

        ops = PhaseOps()
        call_log: list = []

        async def mock_request_reviewer(
            project_id: str, pr_number: int | None
        ) -> ReviewerRequestResult:
            call_log.append((project_id, pr_number))
            return ReviewerRequestResult(requested=True)

        ops.request_reviewer = mock_request_reviewer
        result = await ops._request_reviewer("test-project", 42)

        assert call_log == [("test-project", 42)]
        assert result.requested is True

    @pytest.mark.asyncio
    async def test_request_reviewer_falls_back_to_activity_when_no_callback(
        self,
    ) -> None:
        """When no callback, _request_reviewer invokes the Temporal activity."""
        ops = PhaseOps()
        assert ops.request_reviewer is None
        with pytest.raises(Exception):  # noqa: B017
            await ops._request_reviewer("test-project", 42)


class TestPhaseOpsEmitKpis:
    """Tests for PhaseOps._emit_kpis method."""

    @pytest.mark.asyncio
    async def test_emit_kpis_uses_callback_when_set(self) -> None:
        """When self.emit_kpis callback is set, _emit_kpis calls it directly."""
        from devloop.execution import WorkflowKpiInput

        ops = PhaseOps()
        call_log: list = []

        async def mock_emit_kpis(inp: WorkflowKpiInput) -> None:
            call_log.append(inp)

        ops.emit_kpis = mock_emit_kpis
        inp = WorkflowKpiInput(project_id="p", issue_number=42, ci_fix_iterations=0)
        await ops._emit_kpis(inp)

        assert len(call_log) == 1
        assert call_log[0].issue_number == 42

    @pytest.mark.asyncio
    async def test_emit_kpis_falls_back_to_activity_when_no_callback(self) -> None:
        """When no callback, _emit_kpis invokes the Temporal activity."""
        from devloop.execution import WorkflowKpiInput

        ops = PhaseOps()
        assert ops.emit_kpis is None
        inp = WorkflowKpiInput(project_id="p", issue_number=42)
        with pytest.raises(Exception):  # noqa: B017
            await ops._emit_kpis(inp)


class TestDevLoopWorkflowInheritsOnlyPhaseOps:
    """Verify DevLoopWorkflow inherits only from PhaseOps."""

    def test_devloop_workflow_is_subclass_of_phase_ops(self) -> None:
        """DevLoopWorkflow must inherit from PhaseOps."""
        from devloop.dev_loop import DevLoopWorkflow

        assert issubclass(DevLoopWorkflow, PhaseOps)

    def test_devloop_workflow_single_inheritance(self) -> None:
        """DevLoopWorkflow must have a single base class (PhaseOps)."""
        from devloop.dev_loop import DevLoopWorkflow

        # Only one non-object parent should be present
        real_bases = [b for b in DevLoopWorkflow.__bases__ if b is not object]
        assert len(real_bases) == 1, (
            f"DevLoopWorkflow should inherit from exactly one base, got {real_bases}"
        )
        assert real_bases[0] is PhaseOps

    def test_devloop_workflow_has_phaseops_io_methods(self) -> None:
        """DevLoopWorkflow must have all 5 I/O methods."""
        from devloop.dev_loop import DevLoopWorkflow

        for method_name in (
            "_comment",
            "_dispatch",
            "_cleanup",
            "_request_reviewer",
            "_emit_kpis",
        ):
            assert hasattr(DevLoopWorkflow, method_name), (
                f"DevLoopWorkflow missing {method_name}"
            )

    @pytest.mark.asyncio
    async def test_devloop_workflow_comment_delegates_to_phaseops(
        self,
    ) -> None:
        """When DevLoopWorkflow._comment is called with self.comment set,
        it goes through PhaseOps._comment (callback-first path)."""
        from devloop.dev_loop import DevLoopWorkflow

        wf = DevLoopWorkflow()

        call_log: list = []

        async def mock_comment(project_id: str, issue_number: int, body: str) -> None:
            call_log.append((project_id, issue_number, body))

        wf.comment = mock_comment
        await wf._comment("test-project", 42, "Hello")

        assert call_log == [("test-project", 42, "Hello")]


class TestPRCommentWorkflowExercisesPhaseOps:
    """Verify PRCommentWorkflow delegates to PhaseOps methods."""

    def test_pr_comment_workflow_is_subclass_of_phase_ops(self) -> None:
        """PRCommentWorkflow must inherit from PhaseOps."""
        from devloop.pr_comment import PRCommentWorkflow

        assert issubclass(PRCommentWorkflow, PhaseOps)

    def test_pr_comment_workflow_has_phaseops_io_methods(self) -> None:
        """PRCommentWorkflow must have all 5 I/O methods."""
        from devloop.pr_comment import PRCommentWorkflow

        for method_name in (
            "_comment",
            "_dispatch",
            "_cleanup",
            "_request_reviewer",
            "_emit_kpis",
        ):
            assert hasattr(PRCommentWorkflow, method_name), (
                f"PRCommentWorkflow missing {method_name}"
            )

    @pytest.mark.asyncio
    async def test_pr_comment_workflow_comment_delegates_to_phaseops(
        self,
    ) -> None:
        """When PRCommentWorkflow._comment is called with self.comment set,
        it goes through PhaseOps._comment (callback-first path)."""
        from devloop.pr_comment import PRCommentWorkflow

        wf = PRCommentWorkflow()

        call_log: list = []

        async def mock_comment(project_id: str, issue_number: int, body: str) -> None:
            call_log.append((project_id, issue_number, body))

        wf.comment = mock_comment
        await wf._comment("test-project", 42, "Hello")

        assert call_log == [("test-project", 42, "Hello")]
