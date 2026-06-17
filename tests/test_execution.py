"""Tests for devloop.execution — execution model types.

Covers TaskSpec, AgentJobResult, DispatchInput, OpenAgentPRsInput,
AnswerInput, AwaitInput, PollPRChecksInput, and WorkflowKpiInput.
"""

from __future__ import annotations

import json
from dataclasses import asdict as dataclasses_asdict


class TestTaskSpec:
    """TaskSpec lives in devloop.execution."""

    def test_importable_from_execution_module(self) -> None:
        from devloop.execution import TaskSpec

        spec = TaskSpec(phase="execute", project_id="myrepo")
        assert spec.phase == "execute"
        assert spec.project_id == "myrepo"
        assert spec.issue_number == 0
        assert spec.title == ""
        assert spec.body == ""
        assert spec.branch == ""
        assert spec.instructions == ""
        assert spec.extra == {}

    def test_can_set_all_fields(self) -> None:
        from devloop.execution import TaskSpec

        spec = TaskSpec(
            phase="plan",
            project_id="repo",
            issue_number=42,
            title="Test issue",
            body="Test body",
            branch="feat/test",
            instructions="Do the thing",
            extra={"key": "value"},
        )
        assert spec.phase == "plan"
        assert spec.issue_number == 42
        assert spec.title == "Test issue"
        assert spec.body == "Test body"
        assert spec.branch == "feat/test"
        assert spec.instructions == "Do the thing"
        assert spec.extra == {"key": "value"}

    def test_to_env_value_serializes_to_json(self) -> None:
        from devloop.execution import TaskSpec

        spec = TaskSpec(phase="plan", project_id="repo")
        raw = spec.to_env_value()
        assert isinstance(raw, str)
        d = json.loads(raw)
        assert d["phase"] == "plan"
        assert d["project_id"] == "repo"

    def test_from_env_rebuilds_task_spec(self) -> None:
        from devloop.execution import TaskSpec

        spec = TaskSpec(
            phase="execute",
            project_id="myrepo",
            issue_number=7,
            title="Fix bug",
            instructions="Fix the thing",
            extra={"ci_check_failures": []},
        )
        raw = spec.to_env_value()
        rebuilt = TaskSpec.from_env(raw)

        assert rebuilt.phase == "execute"
        assert rebuilt.project_id == "myrepo"
        assert rebuilt.issue_number == 7
        assert rebuilt.title == "Fix bug"
        assert rebuilt.instructions == "Fix the thing"
        assert rebuilt.extra == {"ci_check_failures": []}

    def test_roundtrip_empty_json(self) -> None:
        from devloop.execution import TaskSpec

        rebuilt = TaskSpec.from_env("")
        assert rebuilt.phase == "execute"
        assert rebuilt.project_id == ""

    def test_to_env_value_empty_extra(self) -> None:
        from devloop.execution import TaskSpec

        spec = TaskSpec(phase="plan", project_id="repo")
        raw = spec.to_env_value()
        d = json.loads(raw)
        assert "extra" in d
        assert d["extra"] == {}


class TestAgentJobResult:
    """AgentJobResult lives in devloop.execution."""

    def test_importable_from_execution_module(self) -> None:
        from devloop.execution import AgentJobResult

        result = AgentJobResult()
        assert result.status == "failed"
        assert result.job_name == ""
        assert result.issue_number == 0
        assert result.branch == ""
        assert result.pr_url == ""
        assert result.commits == 0
        assert result.tests_passed is False
        assert result.question == ""
        assert result.plan is None
        assert result.review is None
        assert result.diagnosis is None
        assert result.summary == ""
        assert result.merged_issues == []
        assert result.merge_commit == ""
        assert result.error == ""

    def test_can_set_all_fields(self) -> None:
        from devloop.execution import AgentJobResult

        result = AgentJobResult(
            status="complete",
            job_name="devloop-job-123",
            issue_number=42,
            branch="feat/test",
            pr_url="https://github.com/repo/pull/42",
            commits=3,
            tests_passed=True,
            question="",
            plan={"issues": []},
            review={"summary": "good"},
            diagnosis={"root_cause": "missing import"},
            summary="Done",
            merged_issues=[42],
            merge_commit="abc123",
            error="",
        )
        assert result.status == "complete"
        assert result.job_name == "devloop-job-123"
        assert result.plan == {"issues": []}
        assert result.review == {"summary": "good"}
        assert result.merged_issues == [42]

    def test_to_payload_drops_job_name(self) -> None:
        from devloop.execution import AgentJobResult

        result = AgentJobResult(job_name="job-123", status="complete")
        payload = result.to_payload()
        assert "job_name" not in payload
        assert payload["status"] == "complete"

    def test_from_payload_rebuilds_with_job_name(self) -> None:
        from devloop.execution import AgentJobResult

        payload = {
            "status": "complete",
            "issue_number": 42,
            "branch": "feat/test",
            "pr_url": "https://example.com/pr/42",
            "commits": 5,
            "tests_passed": True,
            "plan": None,
            "review": {"summary": "ok"},
            "diagnosis": None,
            "summary": "Summary text",
            "merged_issues": [42, 43],
            "merge_commit": "def456",
            "error": "",
        }
        result = AgentJobResult.from_payload(payload, "job-789")
        assert result.status == "complete"
        assert result.job_name == "job-789"
        assert result.issue_number == 42
        assert result.branch == "feat/test"
        assert result.commits == 5
        assert result.tests_passed is True
        assert result.review == {"summary": "ok"}
        assert result.merged_issues == [42, 43]

    def test_roundtrip(self) -> None:
        from devloop.execution import AgentJobResult

        original = AgentJobResult(
            status="complete",
            issue_number=42,
            commits=3,
            review={"summary": "looks good"},
            plan=None,
        )
        payload = original.to_payload()
        rebuilt = AgentJobResult.from_payload(payload, "job-1")

        assert rebuilt.status == original.status
        assert rebuilt.issue_number == original.issue_number
        assert rebuilt.commits == original.commits
        assert rebuilt.review == original.review
        assert rebuilt.plan is None
        assert rebuilt.job_name == "job-1"

    def test_dataclass_serialization(self) -> None:
        from devloop.execution import AgentJobResult

        result = AgentJobResult(status="complete")
        d = dataclasses_asdict(result)
        assert "job_name" in d
        assert d["status"] == "complete"


class TestDispatchInput:
    """DispatchInput lives in devloop.execution."""

    def test_importable_from_execution_module(self) -> None:
        from devloop.execution import DispatchInput, TaskSpec

        spec = TaskSpec(phase="plan", project_id="repo")
        inp = DispatchInput(project_id="repo", issue_number=42, task_spec=spec)
        assert inp.project_id == "repo"
        assert inp.issue_number == 42
        assert inp.task_spec is spec

    def test_test_override_fields(self) -> None:
        from devloop.execution import DispatchInput, TaskSpec

        spec = TaskSpec(phase="plan", project_id="repo")
        inp = DispatchInput(
            project_id="repo",
            issue_number=42,
            task_spec=spec,
            poll_interval_seconds=10.0,
            retention_seconds=600.0,
            image_override="custom:latest",
            omneval_secret_override="my-secret",
            github_url_override="https://github.com/custom/repo",
            github_token_secret_override="my-token-secret",
            service_account_override="my-sa",
        )
        assert inp.poll_interval_seconds == 10.0
        assert inp.retention_seconds == 600.0
        assert inp.image_override == "custom:latest"
        assert inp.omneval_secret_override == "my-secret"
        assert inp.github_url_override == "https://github.com/custom/repo"
        assert inp.github_token_secret_override == "my-token-secret"
        assert inp.service_account_override == "my-sa"


class TestOpenAgentPRsInput:
    """OpenAgentPRsInput lives in devloop.execution."""

    def test_importable_from_execution_module(self) -> None:
        from devloop.execution import OpenAgentPRsInput

        inp = OpenAgentPRsInput(project_id="repo")
        assert inp.project_id == "repo"


class TestAnswerInput:
    """AnswerInput lives in devloop.execution."""

    def test_importable_from_execution_module(self) -> None:
        from devloop.execution import AnswerInput

        inp = AnswerInput(job_name="job-123", answer="Go ahead")
        assert inp.job_name == "job-123"
        assert inp.answer == "Go ahead"


class TestAwaitInput:
    """AwaitInput lives in devloop.execution."""

    def test_importable_from_execution_module(self) -> None:
        from devloop.execution import AwaitInput

        inp = AwaitInput(job_name="job-123")
        assert inp.job_name == "job-123"
        assert inp.poll_interval_seconds == 5.0

    def test_custom_poll_interval(self) -> None:
        from devloop.execution import AwaitInput

        inp = AwaitInput(job_name="job-123", poll_interval_seconds=30.0)
        assert inp.poll_interval_seconds == 30.0


class TestPollPRChecksInput:
    """PollPRChecksInput lives in devloop.execution."""

    def test_importable_from_execution_module(self) -> None:
        from devloop.execution import PollPRChecksInput

        inp = PollPRChecksInput(project_id="repo", pr_number=42)
        assert inp.project_id == "repo"
        assert inp.pr_number == 42

    def test_default_timeout(self) -> None:
        from devloop.execution import PollPRChecksInput

        inp = PollPRChecksInput(project_id="repo", pr_number=42)
        assert inp.timeout_seconds == 300.0

    def test_custom_timeout(self) -> None:
        from devloop.execution import PollPRChecksInput

        inp = PollPRChecksInput(project_id="repo", pr_number=42, timeout_seconds=60.0)
        assert inp.timeout_seconds == 60.0


class TestWorkflowKpiInput:
    """WorkflowKpiInput lives in devloop.execution."""

    def test_importable_from_execution_module(self) -> None:
        from devloop.execution import WorkflowKpiInput

        inp = WorkflowKpiInput(project_id="repo", issue_number=42)
        assert inp.project_id == "repo"
        assert inp.issue_number == 42

    def test_default_values(self) -> None:
        from devloop.execution import WorkflowKpiInput

        inp = WorkflowKpiInput(project_id="repo")
        assert inp.ci_fix_iterations == 0
        assert inp.review_fix_passes == 0
        assert inp.answer_jobs == 0
        assert inp.execute_attempts == 0
        assert inp.review_verdict == ""
        assert inp.label_to_pr_seconds == 0.0
        assert inp.pr_opened is False
        assert inp.commits == 0
        assert inp.ci_exhausted is False

    def test_can_set_all_fields(self) -> None:
        from devloop.execution import WorkflowKpiInput

        inp = WorkflowKpiInput(
            project_id="repo",
            issue_number=42,
            ci_fix_iterations=2,
            review_fix_passes=3,
            answer_jobs=1,
            execute_attempts=5,
            review_verdict="approved",
            label_to_pr_seconds=120.5,
            pr_opened=True,
            commits=4,
            ci_exhausted=False,
        )
        assert inp.ci_fix_iterations == 2
        assert inp.review_fix_passes == 3
        assert inp.answer_jobs == 1
        assert inp.execute_attempts == 5
        assert inp.review_verdict == "approved"
        assert inp.label_to_pr_seconds == 120.5
        assert inp.pr_opened is True
        assert inp.commits == 4
        assert inp.ci_exhausted is False

    def test_dataclass_serialization(self) -> None:
        from devloop.execution import WorkflowKpiInput

        inp = WorkflowKpiInput(project_id="repo", issue_number=42)
        d = dataclasses_asdict(inp)
        assert d["project_id"] == "repo"
        assert d["issue_number"] == 42
        assert "ci_fix_iterations" in d
