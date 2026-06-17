"""Tests for devloop.cichecks — CI check polling types."""

from __future__ import annotations

import dataclasses


class TestCICheckFailure:
    """CICheckFailure dataclass lives in devloop.cichecks."""

    def test_importable_from_cichecks_module(self) -> None:
        from devloop.cichecks import CICheckFailure

        failure = CICheckFailure(name="CI")
        assert failure.name == "CI"
        assert failure.conclusion == ""
        assert failure.details_url == ""
        assert failure.summary == ""

    def test_can_set_all_fields(self) -> None:
        from devloop.cichecks import CICheckFailure

        failure = CICheckFailure(
            name="CI",
            conclusion="failure",
            details_url="https://example.com",
            summary="Something broke",
        )
        assert failure.name == "CI"
        assert failure.conclusion == "failure"
        assert failure.details_url == "https://example.com"
        assert failure.summary == "Something broke"

    def test_dataclass_serialization(self) -> None:
        from devloop.cichecks import CICheckFailure

        failure = CICheckFailure(name="CI", conclusion="failure")
        d = dataclasses.asdict(failure)
        assert d == {
            "name": "CI",
            "conclusion": "failure",
            "details_url": "",
            "summary": "",
        }


class TestCIChecksResult:
    """CIChecksResult dataclass lives in devloop.cichecks."""

    def test_importable_from_cichecks_module(self) -> None:
        from devloop.cichecks import CIChecksResult

        result = CIChecksResult()
        assert result.all_passed is False
        assert result.pending is False
        assert result.failures == []

    def test_all_passed_flag(self) -> None:
        from devloop.cichecks import CIChecksResult

        result = CIChecksResult(all_passed=True)
        assert result.all_passed is True

    def test_pending_flag(self) -> None:
        from devloop.cichecks import CIChecksResult

        result = CIChecksResult(pending=True, all_passed=False)
        assert result.pending is True
        assert result.all_passed is False

    def test_with_failures(self) -> None:
        from devloop.cichecks import CICheckFailure, CIChecksResult

        failures = [CICheckFailure(name="lint"), CICheckFailure(name="test")]
        result = CIChecksResult(all_passed=False, failures=failures)
        assert len(result.failures) == 2
        assert result.failures[0].name == "lint"

    def test_dataclass_serialization(self) -> None:
        from devloop.cichecks import CIChecksResult

        result = CIChecksResult()
        d = dataclasses.asdict(result)
        assert d == {
            "all_passed": False,
            "pending": False,
            "failures": [],
        }


class TestPollCIChecksInput:
    """PollCIChecksInput dataclass lives in devloop.cichecks."""

    def test_importable_from_cichecks_module(self) -> None:
        from devloop.cichecks import PollCIChecksInput

        inp = PollCIChecksInput(project_id="myrepo", pr_number=42)
        assert inp.project_id == "myrepo"
        assert inp.pr_number == 42
