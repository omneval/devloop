"""Tests for _types.py — centralized callback type aliases.

These tests verify that _types.py exists as the single source of truth
for all callback type aliases previously duplicated across phase module
files, and that re-exporting them from phase_ops.py produces identical
types.
"""

from __future__ import annotations




class TestTypesModuleExists:
    """Tests that _types.py exists and exports the expected symbols."""

    def test_types_module_is_importable(self) -> None:
        """_types.py should be importable from devloop.phases."""
        from devloop.phases import _types

        assert _types is not None

    def test_post_comment_callback_exported(self) -> None:
        """_types.py exports _PostCommentCallback."""
        from devloop.phases._types import _PostCommentCallback

        assert _PostCommentCallback is not None

    def test_kpi_bump_callback_exported(self) -> None:
        """_types.py exports _KpiBumpCallback."""
        from devloop.phases._types import _KpiBumpCallback

        assert _KpiBumpCallback is not None

    def test_cleanup_callback_exported(self) -> None:
        """_types.py exports _CleanupCallback."""
        from devloop.phases._types import _CleanupCallback

        assert _CleanupCallback is not None

    def test_dispatch_fix_callback_exported(self) -> None:
        """_types.py exports _DispatchFixCallback."""
        from devloop.phases._types import _DispatchFixCallback

        assert _DispatchFixCallback is not None

    def test_poll_ci_callback_exported(self) -> None:
        """_types.py exports _PollCiCallback."""
        from devloop.phases._types import _PollCiCallback

        assert _PollCiCallback is not None

    def test_dispatch_plan_callback_exported(self) -> None:
        """_types.py exports _DispatchPlanCallback."""
        from devloop.phases._types import _DispatchPlanCallback

        assert _DispatchPlanCallback is not None

    def test_answer_question_callback_exported(self) -> None:
        """_types.py exports _AnswerQuestionCallback."""
        from devloop.phases._types import _AnswerQuestionCallback

        assert _AnswerQuestionCallback is not None

    def test_dispatch_review_callback_exported(self) -> None:
        """_types.py exports _DispatchReviewCallback."""
        from devloop.phases._types import _DispatchReviewCallback

        assert _DispatchReviewCallback is not None

    def test_post_review_findings_callback_exported(self) -> None:
        """_types.py exports _PostReviewFindingsCallback."""
        from devloop.phases._types import _PostReviewFindingsCallback

        assert _PostReviewFindingsCallback is not None

    def test_request_reviewer_callback_exported(self) -> None:
        """_types.py exports _RequestReviewerCallback."""
        from devloop.phases._types import _RequestReviewerCallback

        assert _RequestReviewerCallback is not None

    def test_drop_in_review_callback_exported(self) -> None:
        """_types.py exports _DropInReviewCallback."""
        from devloop.phases._types import _DropInReviewCallback

        assert _DropInReviewCallback is not None

    def test_get_branch_callback_exported(self) -> None:
        """_types.py exports _GetBranchCallback."""
        from devloop.phases._types import _GetBranchCallback

        assert _GetBranchCallback is not None

    def test_dispatch_callback_exported(self) -> None:
        """_types.py exports _DispatchCallback."""
        from devloop.phases._types import _DispatchCallback

        assert _DispatchCallback is not None


class TestTypesIdentity:
    """Tests that types re-exported from phase_ops.py are identical to _types.py."""

    def test_post_comment_callback_identity(self) -> None:
        """_PostCommentCallback from _types.py is identical to phase_ops copy."""
        from devloop.phases._types import _PostCommentCallback as src
        from devloop.phases.phase_ops import _PostCommentCallback as dst

        assert src is dst

    def test_kpi_bump_callback_identity(self) -> None:
        """_KpiBumpCallback from _types.py is identical to phase_ops copy."""
        from devloop.phases._types import _KpiBumpCallback as src
        from devloop.phases.phase_ops import _KpiBumpCallback as dst

        assert src is dst

    def test_cleanup_callback_identity(self) -> None:
        """_CleanupCallback from _types.py is identical to phase_ops copy."""
        from devloop.phases._types import _CleanupCallback as src
        from devloop.phases.phase_ops import _CleanupCallback as dst

        assert src is dst

    def test_dispatch_fix_callback_identity(self) -> None:
        """_DispatchFixCallback from _types.py is identical to phase_ops copy."""
        from devloop.phases._types import _DispatchFixCallback as src
        from devloop.phases.phase_ops import _DispatchFixCallback as dst

        assert src is dst

    def test_poll_ci_callback_identity(self) -> None:
        """_PollCiCallback from _types.py is identical to phase_ops copy."""
        from devloop.phases._types import _PollCiCallback as src
        from devloop.phases.phase_ops import _PollCiCallback as dst

        assert src is dst

    def test_dispatch_plan_callback_identity(self) -> None:
        """_DispatchPlanCallback from _types.py is identical to phase_ops copy."""
        from devloop.phases._types import _DispatchPlanCallback as src
        from devloop.phases.phase_ops import _DispatchPlanCallback as dst

        assert src is dst

    def test_answer_question_callback_identity(self) -> None:
        """_AnswerQuestionCallback from _types.py is identical to phase_ops copy."""
        from devloop.phases._types import _AnswerQuestionCallback as src
        from devloop.phases.phase_ops import _AnswerQuestionCallback as dst

        assert src is dst

    def test_dispatch_review_callback_identity(self) -> None:
        """_DispatchReviewCallback from _types.py is identical to phase_ops copy."""
        from devloop.phases._types import _DispatchReviewCallback as src
        from devloop.phases.review_phase_ops import _DispatchReviewCallback as dst

        assert src is dst

    def test_post_review_findings_callback_identity(self) -> None:
        """_PostReviewFindingsCallback from _types.py is identical to phase_ops copy."""
        from devloop.phases._types import _PostReviewFindingsCallback as src
        from devloop.phases.phase_ops import _PostReviewFindingsCallback as dst

        assert src is dst

    def test_request_reviewer_callback_identity(self) -> None:
        """_RequestReviewerCallback from _types.py is identical to phase_ops copy."""
        from devloop.phases._types import _RequestReviewerCallback as src
        from devloop.phases.phase_ops import _RequestReviewerCallback as dst

        assert src is dst

    def test_drop_in_review_callback_identity(self) -> None:
        """_DropInReviewCallback from _types.py is identical to phase_ops copy."""
        from devloop.phases._types import _DropInReviewCallback as src
        from devloop.phases.phase_ops import _DropInReviewCallback as dst

        assert src is dst

    def test_get_branch_callback_identity(self) -> None:
        """_GetBranchCallback from _types.py is identical to pr_comment copy."""
        from devloop.phases._types import _GetBranchCallback as src
        from devloop.phases.pr_comment import _GetBranchCallback as dst

        assert src is dst

    def test_dispatch_callback_identity(self) -> None:
        """_DispatchCallback from _types.py is identical to pr_comment copy."""
        from devloop.phases._types import _DispatchCallback as src
        from devloop.phases.pr_comment import _DispatchCallback as dst

        assert src is dst


class TestTypesModuleDocstring:
    """Tests that _types.py has a docstring listing all exported aliases."""

    def test_types_module_has_docstring(self) -> None:
        """_types.py should have a module docstring."""
        from devloop.phases import _types

        assert _types.__doc__ is not None, (
            "_types.py must have a docstring listing exported aliases"
        )

    def test_docstring_mentions_post_comment(self) -> None:
        """Docstring should mention _PostCommentCallback."""
        from devloop.phases import _types

        assert "_PostCommentCallback" in _types.__doc__, (
            "Docstring should list _PostCommentCallback"
        )

    def test_docstring_mentions_kpi_bump(self) -> None:
        """Docstring should mention _KpiBumpCallback."""
        from devloop.phases import _types

        assert "_KpiBumpCallback" in _types.__doc__


class TestTypesModuleBackwardCompat:
    """Tests that existing import patterns still work."""

    def test_phase_ops_re_exports_all_types(self) -> None:
        """phase_ops.py should still export all callback types via re-exports."""
        from devloop.phases.phase_ops import (
            _PostCommentCallback,
        )

        assert _PostCommentCallback is not None

    def test_sub_protocol_modules_import_from_types(self) -> None:
        """Sub-protocol modules should import types from _types.py."""
        from devloop.phases.execute_phase_ops import _PostCommentCallback
        from devloop.phases._types import _PostCommentCallback as src

        assert src is _PostCommentCallback

    def test_plan_phase_ops_imports_from_types(self) -> None:
        """plan_phase_ops imports types from _types.py."""
        from devloop.phases.plan_phase_ops import _PostCommentCallback
        from devloop.phases._types import _PostCommentCallback as src

        assert src is _PostCommentCallback

    def test_review_phase_ops_imports_from_types(self) -> None:
        """review_phase_ops imports types from _types.py."""
        from devloop.phases.review_phase_ops import _PostCommentCallback
        from devloop.phases._types import _PostCommentCallback as src

        assert src is _PostCommentCallback

    def test_ci_cycle_ops_imports_from_types(self) -> None:
        """ci_cycle_ops imports types from _types.py."""
        from devloop.phases.ci_cycle_ops import _PostCommentCallback
        from devloop.phases._types import _PostCommentCallback as src

        assert src is _PostCommentCallback

    def test_pr_comment_imports_from_types(self) -> None:
        """pr_comment imports types from _types.py."""
        from devloop.phases.pr_comment import _PostCommentCallback
        from devloop.phases._types import _PostCommentCallback as src

        assert src is _PostCommentCallback
