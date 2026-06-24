"""Tests for devloop.phases.phase_ops — unified PhaseOps callback protocol."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from devloop.phases.phase_ops import PhaseOps
from devloop.cichecks import PollCIChecksInput
from devloop.github import (
    GithubNotificationInput,
    RequestReviewerInput,
)


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
        assert PhaseOps is not None

    def test_has_required_operations(self) -> None:
        """PhaseOps covers the required operations: comment, cleanup, dispatch,
        kpi_bump, poll_ci, request_reviewer."""
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
        instance = PhaseOps.default()
        assert isinstance(instance, PhaseOps)

    def test_default_has_nothing_set(self) -> None:
        """PhaseOps.default() returns an instance with all fields None."""
        instance = PhaseOps.default()
        sig = inspect.signature(PhaseOps.__init__)
        params = {p for p in sig.parameters if p != "self"}
        # The four sub-protocol params are set to their instances (not None).
        sub_protocol_params = {"execute_ops", "review_ops", "ci_ops", "plan_ops"}
        for attr in params:
            if attr in sub_protocol_params:
                continue
            assert getattr(instance, attr) is None, (
                f"Expected {attr} to be None in default()"
            )

    def test_can_set_individual_fields(self) -> None:
        """PhaseOps can be instantiated with individual fields set."""
        callback = lambda *a, **kw: None  # noqa: E731
        instance = PhaseOps(comment=callback)
        assert instance.comment is callback
        assert instance.cleanup is None

    def test_default_is_different_instance(self) -> None:
        """Calling default() twice returns different instances."""
        a = PhaseOps.default()
        b = PhaseOps.default()
        assert a is not b


class TestPhaseOpsReExport:
    """PhaseOps should be re-exported from devloop.phases for convenience."""

    def test_importable_from_phases_package(self) -> None:
        """PhaseOps can be imported from devloop.phases."""
        from devloop.phases import PhaseOps

        assert PhaseOps is not None


# ---------------------------------------------------------------------------
# Functional tests for PhaseOps helper methods (origin/main)
# ---------------------------------------------------------------------------


class TestPhaseOpsAsInt:
    """PhaseOps.as_int — safe int conversion."""

    def test_as_int_with_valid_int(self) -> None:
        """PhaseOps.as_int returns the int as-is."""
        assert PhaseOps().as_int(42) == 42

    def test_as_int_with_string_number(self) -> None:
        """PhaseOps.as_int parses a numeric string."""
        assert PhaseOps().as_int("123") == 123

    def test_as_int_with_non_numeric_string_returns_zero(self) -> None:
        """PhaseOps.as_int returns 0 for non-numeric strings."""
        assert PhaseOps().as_int("abc") == 0

    def test_as_int_with_none_returns_zero(self) -> None:
        """PhaseOps.as_int returns 0 for None."""
        assert PhaseOps().as_int(None) == 0

    def test_as_int_with_float_string_returns_zero(self) -> None:
        """PhaseOps.as_int returns 0 for float-like strings (int() raises)."""
        assert PhaseOps().as_int("3.7") == 0

    def test_as_int_with_float_returns_int(self) -> None:
        """PhaseOps.as_int converts a float to int."""
        assert PhaseOps().as_int(3.7) == 3


class TestPhaseOpsComment:
    """PhaseOps._comment — posts a GitHub Issue / PR comment."""

    @pytest.mark.asyncio
    async def test_comment_calls_callback_when_provided(self) -> None:
        """PhaseOps._comment invokes the injectable callback field."""
        cb = AsyncMock()
        ops = PhaseOps(comment=cb)
        await ops._comment("proj", 42, "hello")
        cb.assert_awaited_once_with("proj", 42, "hello")

    @pytest.mark.asyncio
    async def test_comment_uses_temporal_activity_when_no_callback(self) -> None:
        """PhaseOps._comment falls through to workflow.execute_activity."""
        ops = PhaseOps()
        activity_args: list[tuple] = []

        async def fake_execute_activity(name, payload, **kwargs):  # noqa: ANN001, ANN002
            activity_args.append((name, payload, kwargs))
            return None

        with patch(
            "devloop.phases.phase_ops.workflow.execute_activity",
            fake_execute_activity,
        ):
            await ops._comment("proj", 42, "hello")

        assert len(activity_args) == 1
        name, payload, kwargs = activity_args[0]
        assert name == "post_github_comment"
        assert isinstance(payload, GithubNotificationInput)
        assert payload.project_id == "proj"
        assert payload.issue_number == 42
        assert payload.body == "hello"


class TestPhaseOpsCleanup:
    """PhaseOps._cleanup — deletes output ConfigMap."""

    @pytest.mark.asyncio
    async def test_cleanup_calls_callback_when_provided(self) -> None:
        """PhaseOps._cleanup invokes the injectable callback field."""
        cb = AsyncMock()
        ops = PhaseOps(cleanup=cb)
        await ops._cleanup("my-job")
        cb.assert_awaited_once_with("my-job")

    @pytest.mark.asyncio
    async def test_cleanup_uses_temporal_when_no_callback(self) -> None:
        """PhaseOps._cleanup falls through to workflow.execute_activity."""
        ops = PhaseOps()
        activity_called = []

        async def fake_act(name, payload, **kwargs):  # noqa: ANN001, ANN002
            activity_called.append((name, payload, kwargs))
            return None

        with patch(
            "devloop.phases.phase_ops.workflow.execute_activity",
            fake_act,
        ):
            await ops._cleanup("my-job")

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
        returned = await PhaseOps.dispatch_helper(
            PhaseOps(),
            "proj",
            mock_spec,
            42,
            5.0,
            dispatch_callback=cb,
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
            await PhaseOps.dispatch_helper(
                PhaseOps(),
                "proj",
                MagicMock(),
                42,
                5.0,
                dispatch_callback=None,
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


class TestPhaseOpsPoll:
    """PhaseOps.poll — polls CI checks via activity or callback."""

    @pytest.mark.asyncio
    async def test_poll_calls_callback_when_provided(self) -> None:
        """PhaseOps.poll invokes the callback directly."""
        mock_result = MagicMock(all_passed=True, failures=[])
        cb = AsyncMock(return_value=mock_result)
        result = await PhaseOps.poll(PhaseOps(), "proj", 42, callback=cb)
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
            result = await PhaseOps.poll(PhaseOps(), "proj", 42, callback=None)

        assert len(activity_called) == 1
        name, payload, kwargs = activity_called[0]
        assert name == "poll_ci_checks"
        assert isinstance(payload, PollCIChecksInput)
        assert payload.project_id == "proj"
        assert payload.pr_number == 42
        assert result.all_passed is True


class TestPhaseOpsRequestReviewer:
    """PhaseOps._request_reviewer — requests a GitHub PR reviewer."""

    @pytest.mark.asyncio
    async def test_request_reviewer_calls_callback_when_provided(self) -> None:
        """PhaseOps._request_reviewer invokes the injectable callback field."""
        mock_result = MagicMock(requested=True, reason=None)
        cb = AsyncMock(return_value=mock_result)
        ops = PhaseOps(request_reviewer=cb)
        result = await ops._request_reviewer("proj", 42)
        cb.assert_awaited_once_with("proj", 42)
        assert result.requested is True

    @pytest.mark.asyncio
    async def test_request_reviewer_uses_temporal_when_no_callback(self) -> None:
        """PhaseOps._request_reviewer falls through to workflow.execute_activity."""
        activity_called = []
        ops = PhaseOps()

        async def fake_act(name, payload, **kwargs):  # noqa: ANN001, ANN002
            activity_called.append((name, payload, kwargs))
            return MagicMock(requested=True, reason=None)

        with patch(
            "devloop.phases.phase_ops.workflow.execute_activity",
            fake_act,
        ):
            result = await ops._request_reviewer("proj", 42)

        assert len(activity_called) == 1
        name, payload, kwargs = activity_called[0]
        assert name == "request_github_reviewer"
        assert isinstance(payload, RequestReviewerInput)
        assert payload.project_id == "proj"
        assert payload.pr_number == 42
        assert result.requested is True
