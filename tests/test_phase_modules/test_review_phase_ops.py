"""Tests for ReviewPhaseOps — the focused review-phase callback protocol."""

from __future__ import annotations

import inspect
from typing import Any


from devloop.phases.phase_ops import PhaseOps


class TestReviewPhaseOpsFields:
    """ReviewPhaseOps exposes exactly 4 fields."""

    def test_has_comment_field(self) -> None:
        """ReviewPhaseOps has a comment field."""
        from devloop.phases.review_phase_ops import ReviewPhaseOps

        ops = ReviewPhaseOps()
        assert hasattr(ops, "comment")
        assert ops.comment is None

    def test_has_dispatch_review_field(self) -> None:
        """ReviewPhaseOps has a dispatch_review field."""
        from devloop.phases.review_phase_ops import ReviewPhaseOps

        ops = ReviewPhaseOps()
        assert hasattr(ops, "dispatch_review")
        assert ops.dispatch_review is None

    def test_has_post_review_findings_field(self) -> None:
        """ReviewPhaseOps has a post_review_findings field."""
        from devloop.phases.review_phase_ops import ReviewPhaseOps

        ops = ReviewPhaseOps()
        assert hasattr(ops, "post_review_findings")
        assert ops.post_review_findings is None

    def test_has_cleanup_field(self) -> None:
        """ReviewPhaseOps has a cleanup field."""
        from devloop.phases.review_phase_ops import ReviewPhaseOps

        ops = ReviewPhaseOps()
        assert hasattr(ops, "cleanup")
        assert ops.cleanup is None

    def test_has_exactly_four_fields(self) -> None:
        """ReviewPhaseOps has exactly 4 data fields."""
        from devloop.phases.review_phase_ops import ReviewPhaseOps

        sig = inspect.signature(ReviewPhaseOps.__init__)
        params = {p for p in sig.parameters if p not in ("self", "kwargs")}
        assert params == {
            "comment",
            "dispatch_review",
            "post_review_findings",
            "cleanup",
        }

    def test_can_set_all_fields(self) -> None:
        """ReviewPhaseOps can be instantiated with all fields."""
        from devloop.phases.review_phase_ops import ReviewPhaseOps

        async def noop(*a: Any, **kw: Any) -> None:
            pass

        ops = ReviewPhaseOps(
            comment=noop,
            dispatch_review=noop,
            post_review_findings=noop,
            cleanup=noop,
        )
        assert ops.comment is noop
        assert ops.dispatch_review is noop
        assert ops.post_review_findings is noop
        assert ops.cleanup is noop


class TestReviewPhaseOpsDefault:
    """ReviewPhaseOps.default() returns an instance with all None."""

    def test_default_returns_instance(self) -> None:
        """ReviewPhaseOps.default() returns a ReviewPhaseOps instance."""
        from devloop.phases.review_phase_ops import ReviewPhaseOps

        ops = ReviewPhaseOps.default()
        assert isinstance(ops, ReviewPhaseOps)

    def test_default_all_none(self) -> None:
        """ReviewPhaseOps.default() sets all fields to None."""
        from devloop.phases.review_phase_ops import ReviewPhaseOps

        ops = ReviewPhaseOps.default()
        assert ops.comment is None
        assert ops.dispatch_review is None
        assert ops.post_review_findings is None
        assert ops.cleanup is None


class TestReviewPhaseOpsBackwardCompat:
    """PhaseOps exposes review_ops sub-protocol for backward compatibility."""

    def test_phaseops_has_review_ops(self) -> None:
        """PhaseOps exposes a review_ops property returning the sub-protocol."""
        from devloop.phases.review_phase_ops import ReviewPhaseOps

        ops = PhaseOps()
        assert hasattr(ops, "review_ops")
        assert isinstance(ops.review_ops, ReviewPhaseOps)

    def test_phaseops_review_ops_has_expected_fields(self) -> None:
        """PhaseOps.review_ops has the same fields as ReviewPhaseOps."""

        phase_ops = PhaseOps()
        for field in ("comment", "dispatch_review", "post_review_findings", "cleanup"):
            assert hasattr(phase_ops.review_ops, field)


class TestReviewPhaseOpsInPhaseOps:
    """PhaseOps.__init__ packages review fields into review_ops."""

    def test_phaseops_packages_review_fields(self) -> None:
        """PhaseOps constructor packages review-specific fields into review_ops."""
        from devloop.phases.review_phase_ops import ReviewPhaseOps

        async def noop(*a: Any, **kw: Any) -> None:
            pass

        ops = PhaseOps(
            comment=noop,
            dispatch_review=noop,
            post_review_findings=noop,
            cleanup=noop,
        )
        assert isinstance(ops.review_ops, ReviewPhaseOps)
        assert ops.review_ops.comment is noop
        assert ops.review_ops.dispatch_review is noop
        assert ops.review_ops.post_review_findings is noop
        assert ops.review_ops.cleanup is noop
