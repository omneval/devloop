<<<<<<< HEAD
"""Tests for devloop.phases.phase_ops — unified PhaseOps callback protocol."""

from __future__ import annotations

import inspect


# All known PhaseOps attribute names (data attributes + properties + classmethods).
_KNOWN_ATTRS = frozenset(
    {
        # Core operations
        "comment",
        "cleanup",
        "dispatch",
        "kpi_bump",
        "poll_ci",
        "request_reviewer",
        # ExecutePhase
        "dispatch_execute",
        "answer_question",
        # ReviewPhase
        "dispatch_review",
        "post_review_findings",
        # PlanPhase
        "plan_issue",
        "dispatch_plan",
        "drop_issues_in_review",
        # KPI emission
        "kpi_take",
        "emit_kpis",
        # Backward-compat aliases
        "post_comment",
        "phaseops",
    }
)


class TestPhaseOpsProtocol:
    """PhaseOps — the unified I/O adapter protocol for all phase modules."""

    def test_importable_from_phases_module(self) -> None:
        """PhaseOps can be imported from devloop.phases.phase_ops."""
        from devloop.phases.phase_ops import PhaseOps

        assert PhaseOps is not None

    def test_has_required_operations(self) -> None:
        """PhaseOps covers the required operations: comment, cleanup, dispatch,
        kpi_bump, poll_ci, request_reviewer."""
        from devloop.phases.phase_ops import PhaseOps

        # Check constructor parameter names
        sig = inspect.signature(PhaseOps.__init__)
        params = {p for p in sig.parameters if p != "self"}
        required = {
            "comment",
            "cleanup",
            "dispatch",
            "kpi_bump",
            "poll_ci",
            "request_reviewer",
        }
        for req in required:
            assert req in params, (
                f"PhaseOps.__init__ is missing required operation: {req}"
            )

    def test_has_phase_specific_operations(self) -> None:
        """PhaseOps covers phase-specific operations so that any phase can use
        the same protocol without needing its own dataclass."""
        from devloop.phases.phase_ops import PhaseOps

        sig = inspect.signature(PhaseOps.__init__)
        params = {p for p in sig.parameters if p != "self"}
        phase_specific = {
            # ExecutePhase
            "dispatch_execute",
            "answer_question",
            # ReviewPhase
            "dispatch_review",
            "post_review_findings",
            # CICycle (maps to dispatch)
            # ReviewFixPass (maps to dispatch)
            # PlanPhase
            "plan_issue",
            "dispatch_plan",
            "drop_issues_in_review",
            # KPI emission
            "kpi_take",
            "emit_kpis",
            # Notifier
        }
        for ps in phase_specific:
            assert ps in params, (
                f"PhaseOps.__init__ is missing phase-specific operation: {ps}"
            )

    def test_has_default_classmethod(self) -> None:
        """PhaseOps has a default() classmethod that returns an instance."""
        from devloop.phases.phase_ops import PhaseOps

        instance = PhaseOps.default()
        assert isinstance(instance, PhaseOps)

    def test_default_has_nothing_set(self) -> None:
        """PhaseOps.default() returns an instance with all fields None."""
        from devloop.phases.phase_ops import PhaseOps

        instance = PhaseOps.default()
        sig = inspect.signature(PhaseOps.__init__)
        params = {p for p in sig.parameters if p != "self"}
        for attr in params:
            assert getattr(instance, attr) is None, (
                f"Expected {attr} to be None in default()"
            )

    def test_can_set_individual_fields(self) -> None:
        """PhaseOps can be instantiated with individual fields set."""
        from devloop.phases.phase_ops import PhaseOps

        callback = lambda *a, **kw: None  # noqa: E731
        instance = PhaseOps(comment=callback)
        assert instance.comment is callback
        assert instance.cleanup is None

    def test_default_is_different_instance(self) -> None:
        """Calling default() twice returns different instances."""
        from devloop.phases.phase_ops import PhaseOps

        a = PhaseOps.default()
        b = PhaseOps.default()
        assert a is not b


class TestPhaseOpsReExport:
    """PhaseOps should be re-exported from devloop.phases for convenience."""

    def test_importable_from_phases_package(self) -> None:
        """PhaseOps can be imported from devloop.phases."""
        from devloop.phases import PhaseOps

        assert PhaseOps is not None
=======
"""Unit tests for devloop.phases.phase_ops — PhaseOps shared module."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from devloop.phases.phase_ops import PhaseOps
from devloop.shared import (
    GithubNotificationInput,
    PollCIChecksInput,
    RequestReviewerInput,
)


class TestPhaseOpsAsInt:
    """PhaseOps.as_int — safe int conversion."""

    def test_as_int_with_valid_int(self) -> None:
        """PhaseOps.as_int returns the int as-is."""
        from devloop.phases.phase_ops import PhaseOps

        assert PhaseOps().as_int(42) == 42

    def test_as_int_with_string_number(self) -> None:
        """PhaseOps.as_int parses a numeric string."""
        from devloop.phases.phase_ops import PhaseOps

        assert PhaseOps().as_int("123") == 123

    def test_as_int_with_non_numeric_string_returns_zero(self) -> None:
        """PhaseOps.as_int returns 0 for non-numeric strings."""
        from devloop.phases.phase_ops import PhaseOps

        assert PhaseOps().as_int("abc") == 0

    def test_as_int_with_none_returns_zero(self) -> None:
        """PhaseOps.as_int returns 0 for None."""
        from devloop.phases.phase_ops import PhaseOps

        assert PhaseOps().as_int(None) == 0

    def test_as_int_with_float_string_returns_zero(self) -> None:
        """PhaseOps.as_int returns 0 for float-like strings (int() raises)."""
        from devloop.phases.phase_ops import PhaseOps

        assert PhaseOps().as_int("3.7") == 0

    def test_as_int_with_float_returns_int(self) -> None:
        """PhaseOps.as_int converts a float to int."""
        assert PhaseOps().as_int(3.7) == 3


class TestPhaseOpsComment:
    """PhaseOps.comment — posts a GitHub Issue / PR comment."""

    @pytest.mark.asyncio
    async def test_comment_calls_callback_when_provided(self) -> None:
        """PhaseOps.comment invokes the callback directly."""
        cb = AsyncMock()
        await PhaseOps().comment("proj", 42, "hello", callback=cb)
        cb.assert_awaited_once_with("proj", 42, "hello")

    @pytest.mark.asyncio
    async def test_comment_uses_temporal_activity_when_no_callback(self) -> None:
        """PhaseOps.comment falls through to workflow.execute_activity."""
        ops = PhaseOps()
        activity_args: list[tuple] = []

        async def fake_execute_activity(name, payload, **kwargs):  # noqa: ANN001, ANN002
            activity_args.append((name, payload, kwargs))
            return None

        with patch(
            "devloop.phases.phase_ops.workflow.execute_activity",
            fake_execute_activity,
        ):
            await ops.comment("proj", 42, "hello")

        assert len(activity_args) == 1
        name, payload, kwargs = activity_args[0]
        assert name == "post_github_comment"
        assert isinstance(payload, GithubNotificationInput)
        assert payload.project_id == "proj"
        assert payload.issue_number == 42
        assert payload.body == "hello"

    @pytest.mark.asyncio
    async def test_cleanup_calls_callback_when_provided(self) -> None:
        """PhaseOps.cleanup invokes the callback directly."""
        cb = AsyncMock()
        await PhaseOps().cleanup("some-job", callback=cb)
        cb.assert_awaited_once_with("some-job")

    @pytest.mark.asyncio
    async def test_cleanup_empty_job_name_returns_early(self) -> None:
        """PhaseOps.cleanup does nothing when job_name is empty."""
        activity_called = []

        async def fake_act(name, payload, **kwargs):
            activity_called.append(True)

        with patch(
            "devloop.phases.phase_ops.workflow.execute_activity",
            fake_act,
        ):
            await PhaseOps().cleanup("", callback=None)

        assert len(activity_called) == 0

    @pytest.mark.asyncio
    async def test_cleanup_uses_temporal_activity_when_no_callback(self) -> None:
        """PhaseOps.cleanup falls through to workflow.execute_activity."""
        activity_called = []

        async def fake_act(name, payload, **kwargs):
            activity_called.append((name, payload, kwargs))

        with patch(
            "devloop.phases.phase_ops.workflow.execute_activity",
            fake_act,
        ):
            await PhaseOps().cleanup("my-job", callback=None)

        assert len(activity_called) == 1
        name, payload, kwargs = activity_called[0]
        assert name == "cleanup_configmap"
        assert payload == "my-job"


class TestPhaseOpsDispatch:
    """PhaseOps.dispatch_helper — generic dispatch with callback fallback."""

    @pytest.mark.asyncio
    async def test_dispatch_calls_callback_when_provided(self) -> None:
        """PhaseOps.dispatch_helper invokes the callback directly."""
        mock_result = MagicMock(status="complete", job_name="test-job", commits=0)
        mock_spec = MagicMock()
        cb = AsyncMock(return_value=mock_result)
        returned = await PhaseOps().dispatch_helper(
            "proj", mock_spec, 42, 5.0, dispatch_callback=cb
        )
        cb.assert_awaited_once_with("proj", mock_spec, 42, 5.0)
        assert returned.status == "complete"

    @pytest.mark.asyncio
    async def test_dispatch_uses_temporal_activity_when_no_callback(self) -> None:
        """PhaseOps.dispatch_helper falls through to workflow.execute_activity."""
        activity_called = []

        class _FakeResult:
            status = "complete"
            job_name = "my-job"
            commits = 0
            branch = "feat/1"

        async def fake_act(name, payload, **kwargs):  # noqa: ANN001, ANN002
            activity_called.append((name, payload, kwargs))
            return _FakeResult()

        with patch(
            "devloop.phases.phase_ops.workflow.execute_activity",
            fake_act,
        ):
            await PhaseOps().dispatch_helper(
                "proj", MagicMock(), 42, 5.0, dispatch_callback=None
            )

        assert len(activity_called) == 1
        name, payload, kwargs = activity_called[0]
        assert name == "dispatch_agent_job"
        assert payload.project_id == "proj"
        assert payload.issue_number == 42


class TestPhaseOpsPrNumberFromUrl:
    """PhaseOps.pr_number_from_url — safe PR number extraction from URL."""

    def test_pr_number_from_github_pr_url(self) -> None:
        """PhaseOps.pr_number_from_url extracts the PR number from a GitHub URL."""
        assert (
            PhaseOps.pr_number_from_url("https://github.com/omneval/omneval/pull/42")
            == 42
        )

    def test_pr_number_from_github_pr_url_with_trailing_slash(self) -> None:
        """PhaseOps.pr_number_from_url handles trailing slash."""
        assert (
            PhaseOps.pr_number_from_url("https://github.com/omneval/omneval/pull/99/")
            == 99
        )

    def test_pr_number_from_url_no_pr_returns_zero(self) -> None:
        """PhaseOps.pr_number_from_url returns 0 for non-PR URLs."""
        assert (
            PhaseOps.pr_number_from_url("https://github.com/omneval/omneval/issues/42")
            == 0
        )

    def test_pr_number_from_url_empty_string_returns_zero(self) -> None:
        """PhaseOps.pr_number_from_url returns 0 for empty string."""
        assert PhaseOps.pr_number_from_url("") == 0

    def test_pr_number_from_url_none_returns_zero(self) -> None:
        """PhaseOps.pr_number_from_url returns 0 for None."""
        assert PhaseOps.pr_number_from_url(None) == 0  # type: ignore[arg-type]


class TestPhaseOpsDispatchActivity:
    """PhaseOps.dispatch_activity — generic activity dispatch with callback fallback."""

    @pytest.mark.asyncio
    async def test_dispatch_activity_calls_callback_when_provided(self) -> None:
        """PhaseOps.dispatch_activity invokes the callback directly."""
        mock_result = MagicMock(status="ok")
        cb = AsyncMock(return_value=mock_result)
        result = await PhaseOps().dispatch_activity(
            "custom_activity",
            {"key": "value"},
            callback=cb,
        )
        cb.assert_awaited_once_with({"key": "value"})
        assert result.status == "ok"

    @pytest.mark.asyncio
    async def test_dispatch_activity_uses_temporal_when_no_callback(self) -> None:
        """PhaseOps.dispatch_activity falls through to workflow.execute_activity."""
        activity_called = []

        async def fake_act(name, payload, **kwargs):  # noqa: ANN001, ANN002
            activity_called.append((name, payload, kwargs))
            return MagicMock(status="ok")

        with patch(
            "devloop.phases.phase_ops.workflow.execute_activity",
            fake_act,
        ):
            result = await PhaseOps().dispatch_activity(
                "custom_activity",
                {"key": "value"},
                callback=None,
                timeout=timedelta(seconds=30),
            )

        assert len(activity_called) == 1
        name, payload, kwargs = activity_called[0]
        assert name == "custom_activity"
        assert payload == {"key": "value"}
        assert result.status == "ok"


class TestPhaseOpsPoll:
    """PhaseOps.poll — polls CI checks via activity or callback."""

    @pytest.mark.asyncio
    async def test_poll_calls_callback_when_provided(self) -> None:
        """PhaseOps.poll invokes the callback directly."""
        mock_result = MagicMock(all_passed=True, failures=[])
        cb = AsyncMock(return_value=mock_result)
        result = await PhaseOps().poll("proj", 42, callback=cb)
        cb.assert_awaited_once_with("proj", 42)
        assert result.all_passed is True

    @pytest.mark.asyncio
    async def test_poll_uses_temporal_when_no_callback(self) -> None:
        """PhaseOps.poll falls through to workflow.execute_activity for poll_ci_checks."""
        activity_called = []

        async def fake_act(name, payload, **kwargs):  # noqa: ANN001, ANN002
            activity_called.append((name, payload, kwargs))
            return MagicMock(all_passed=True, failures=[])

        with patch(
            "devloop.phases.phase_ops.workflow.execute_activity",
            fake_act,
        ):
            result = await PhaseOps().poll("proj", 42, callback=None)

        assert len(activity_called) == 1
        name, payload, kwargs = activity_called[0]
        assert name == "poll_ci_checks"
        assert isinstance(payload, PollCIChecksInput)
        assert payload.project_id == "proj"
        assert payload.pr_number == 42
        assert result.all_passed is True


class TestPhaseOpsRequestReviewer:
    """PhaseOps.request_reviewer — requests a GitHub PR reviewer."""

    @pytest.mark.asyncio
    async def test_request_reviewer_calls_callback_when_provided(self) -> None:
        """PhaseOps.request_reviewer invokes the callback directly."""
        mock_result = MagicMock(requested=True, reason=None)
        cb = AsyncMock(return_value=mock_result)
        result = await PhaseOps().request_reviewer("proj", 42, callback=cb)
        cb.assert_awaited_once_with("proj", 42)
        assert result.requested is True

    @pytest.mark.asyncio
    async def test_request_reviewer_uses_temporal_when_no_callback(self) -> None:
        """PhaseOps.request_reviewer falls through to workflow.execute_activity."""
        activity_called = []

        async def fake_act(name, payload, **kwargs):  # noqa: ANN001, ANN002
            activity_called.append((name, payload, kwargs))
            return MagicMock(requested=True, reason=None)

        with patch(
            "devloop.phases.phase_ops.workflow.execute_activity",
            fake_act,
        ):
            result = await PhaseOps().request_reviewer("proj", 42, callback=None)

        assert len(activity_called) == 1
        name, payload, kwargs = activity_called[0]
        assert name == "request_github_reviewer"
        assert isinstance(payload, RequestReviewerInput)
        assert payload.project_id == "proj"
        assert payload.pr_number == 42
        assert result.requested is True
>>>>>>> origin/main
