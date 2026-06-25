"""Tests for QualityGate — pure-function decision module.

This module contains no I/O and no Temporal dependencies.
It simply evaluates whether a sentrux score meets a threshold.
"""

from __future__ import annotations


from devloop.phases.quality_gate import QualityGate


class TestQualityGateCheck:
    """Tests for QualityGate.check(score, threshold) -> 'pass' | 'fail'."""

    def test_pass_when_score_equals_threshold(self):
        """Score exactly equal to threshold should pass."""
        assert QualityGate.check(7000, 7000) == "pass"

    def test_pass_when_score_exceeds_threshold(self):
        """Score above threshold should pass."""
        assert QualityGate.check(8000, 7000) == "pass"

    def test_fail_when_score_below_threshold(self):
        """Score below threshold should fail."""
        assert QualityGate.check(5000, 7000) == "fail"

    def test_fail_when_score_is_zero(self):
        """Zero score with non-zero threshold should fail."""
        assert QualityGate.check(0, 7000) == "fail"

    def test_pass_with_zero_threshold(self):
        """Any score with zero threshold should pass."""
        assert QualityGate.check(0, 0) == "pass"

    def test_pass_with_max_score(self):
        """Maximum score (10000) should pass against any reasonable threshold."""
        assert QualityGate.check(10000, 7000) == "pass"

    def test_fail_with_one_point_below_threshold(self):
        """Single point below threshold should fail."""
        assert QualityGate.check(6999, 7000) == "fail"
