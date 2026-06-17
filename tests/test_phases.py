"""Tests for devloop.phases — Phase and JobStatus enums."""

from __future__ import annotations


class TestPhase:
    """Phase enum lives in devloop.phases."""

    def test_importable_from_phases_module(self) -> None:
        from devloop.phases import Phase

        # Verify all expected phases exist with correct values.
        assert Phase.PLAN.value == "plan"
        assert Phase.EXECUTE.value == "execute"
        assert Phase.REVIEW.value == "review"
        assert Phase.DIAGNOSIS.value == "diagnosis"
        assert Phase.CI_FIX.value == "ci_fix"
        assert Phase.SUMMARIZE.value == "summarize"
        assert Phase.ANSWER.value == "answer"
        assert Phase.PR_COMMENT.value == "pr_comment"
        assert Phase.CODE_QUALITY_SCAN.value == "code_quality_scan"
        assert Phase.CODE_QUALITY_IMPROVE.value == "code_quality_improve"

    def test_is_string_enum(self) -> None:
        from devloop.phases import Phase

        # Phase is a str enum, so direct comparison with strings works.
        assert Phase.PLAN == "plan"
        assert "plan" == Phase.PLAN

    def test_iteration(self) -> None:
        from devloop.phases import Phase

        members = list(Phase)
        assert len(members) == 10
        assert Phase.PLAN in members


class TestJobStatus:
    """JobStatus enum lives in devloop.phases."""

    def test_importable_from_phases_module(self) -> None:
        from devloop.phases import JobStatus

        assert JobStatus.COMPLETE.value == "complete"
        assert JobStatus.FAILED.value == "failed"
        assert JobStatus.AWAITING_HUMAN.value == "awaiting_human"

    def test_is_string_enum(self) -> None:
        from devloop.phases import JobStatus

        assert JobStatus.COMPLETE == "complete"
        assert "complete" == JobStatus.COMPLETE

    def test_iteration(self) -> None:
        from devloop.phases import JobStatus

        members = list(JobStatus)
        assert len(members) == 3
        assert JobStatus.COMPLETE in members
