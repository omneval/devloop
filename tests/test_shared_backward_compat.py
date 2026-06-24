"""Tests for backward-compatible imports via devloop.shared.

These ensure every type that used to live in shared.py is still importable
from ``from devloop.shared import ...`` after the split into sub-modules.
"""

from __future__ import annotations


class TestSharedConstants:
    """Constants defined directly in shared.py."""

    def test_orchestration_queue(self) -> None:
        from devloop.shared import ORCHESTRATION_QUEUE

        assert isinstance(ORCHESTRATION_QUEUE, str)
        assert len(ORCHESTRATION_QUEUE) > 0

    def test_job_dispatch_queue(self) -> None:
        from devloop.shared import JOB_DISPATCH_QUEUE

        assert isinstance(JOB_DISPATCH_QUEUE, str)
        assert len(JOB_DISPATCH_QUEUE) > 0

    def test_key_result(self) -> None:
        from devloop.shared import KEY_RESULT

        assert KEY_RESULT == "result"

    def test_key_human_answer(self) -> None:
        from devloop.shared import KEY_HUMAN_ANSWER

        assert KEY_HUMAN_ANSWER == "human_answer"


class TestSharedPhaseJobStatusBackwardCompat:
    """Phase and JobStatus re-exported from shared.py."""

    def test_phase_from_shared(self) -> None:
        from devloop.shared import Phase

        assert Phase.PLAN == "plan"

    def test_job_status_from_shared(self) -> None:
        from devloop.shared import JobStatus

        assert JobStatus.COMPLETE == "complete"


class TestSharedExecutionBackwardCompat:
    """Execution types re-exported from shared.py."""

    def test_task_spec_from_shared(self) -> None:
        from devloop.shared import TaskSpec

        spec = TaskSpec(phase="execute", project_id="repo")
        assert spec.phase == "execute"
        assert spec.project_id == "repo"

    def test_agent_job_result_from_shared(self) -> None:
        from devloop.shared import AgentJobResult

        result = AgentJobResult()
        assert result.status == "failed"

    def test_dispatch_input_from_shared(self) -> None:
        from devloop.shared import DispatchInput, TaskSpec

        spec = TaskSpec(phase="plan", project_id="repo")
        inp = DispatchInput(project_id="repo", issue_number=42, task_spec=spec)
        assert inp.project_id == "repo"

    def test_open_agent_prs_input_from_shared(self) -> None:
        from devloop.shared import OpenAgentPRsInput

        inp = OpenAgentPRsInput(project_id="repo")
        assert inp.project_id == "repo"

    def test_answer_input_from_shared(self) -> None:
        from devloop.shared import AnswerInput

        inp = AnswerInput(job_name="job-1", answer="go")
        assert inp.job_name == "job-1"

    def test_await_input_from_shared(self) -> None:
        from devloop.shared import AwaitInput

        inp = AwaitInput(job_name="job-1")
        assert inp.job_name == "job-1"

    def test_poll_pr_checks_input_from_shared(self) -> None:
        from devloop.shared import PollPRChecksInput

        inp = PollPRChecksInput(project_id="repo", pr_number=42)
        assert inp.project_id == "repo"

    def test_workflow_kpi_input_from_shared(self) -> None:
        from devloop.shared import WorkflowKpiInput

        inp = WorkflowKpiInput(project_id="repo")
        assert inp.project_id == "repo"


class TestSharedGithubBackwardCompat:
    """GitHub types re-exported from shared.py."""

    def test_inline_comment_from_shared(self) -> None:
        from devloop.shared import InlineComment

        c = InlineComment(file="main.py", line=10, body="fix")
        assert c.file == "main.py"

    def test_post_comments_input_from_shared(self) -> None:
        from devloop.shared import PostCommentsInput

        inp = PostCommentsInput(project_id="repo", pr_number=42, summary="s")
        assert inp.project_id == "repo"

    def test_github_notification_input_from_shared(self) -> None:
        from devloop.shared import GithubNotificationInput

        inp = GithubNotificationInput(issue_number=42, project_id="repo", body="hello")
        assert inp.project_id == "repo"

    def test_request_reviewer_input_from_shared(self) -> None:
        from devloop.shared import RequestReviewerInput

        inp = RequestReviewerInput(project_id="repo", pr_number=42, reviewer="alice")
        assert inp.project_id == "repo"

    def test_reviewer_request_result_from_shared(self) -> None:
        from devloop.shared import ReviewerRequestResult

        r = ReviewerRequestResult()
        assert r.requested is False

    def test_get_pr_branch_input_from_shared(self) -> None:
        from devloop.shared import GetPRBranchInput

        inp = GetPRBranchInput(project_id="repo", pr_number=42)
        assert inp.project_id == "repo"

    def test_get_pr_diff_input_from_shared(self) -> None:
        from devloop.shared import GetPRDiffInput

        inp = GetPRDiffInput(project_id="repo", pr_number=42)
        assert inp.project_id == "repo"

    def test_create_github_issue_input_from_shared(self) -> None:
        from devloop.shared import CreateGithubIssueInput

        inp = CreateGithubIssueInput(
            project_id="repo", title="t", body="b", labels=["l"]
        )
        assert inp.project_id == "repo"

    def test_update_github_issue_input_from_shared(self) -> None:
        from devloop.shared import UpdateGithubIssueInput

        inp = UpdateGithubIssueInput(project_id="repo", issue_number=42)
        assert inp.project_id == "repo"

    def test_publish_summary_input_from_shared(self) -> None:
        from devloop.shared import PublishSummaryInput

        inp = PublishSummaryInput(project_id="repo", summary="s", date="2026-06-06")
        assert inp.project_id == "repo"

    def test_plan_issue_input_from_shared(self) -> None:
        from devloop.shared import PlanIssueInput

        inp = PlanIssueInput(project_id="repo", issue_number=42)
        assert inp.project_id == "repo"


class TestSharedCichecksBackwardCompat:
    """CI check types re-exported from shared.py."""

    def test_cicheck_failure_from_shared(self) -> None:
        from devloop.shared import CICheckFailure

        f = CICheckFailure(name="CI")
        assert f.name == "CI"

    def test_cichecks_result_from_shared(self) -> None:
        from devloop.shared import CIChecksResult

        r = CIChecksResult()
        assert r.all_passed is False

    def test_poll_cichecks_input_from_shared(self) -> None:
        from devloop.shared import PollCIChecksInput

        inp = PollCIChecksInput(project_id="repo", pr_number=42)
        assert inp.project_id == "repo"


class TestSharedModuleIndependence:
    """Importing sub-modules does not trigger other sub-module imports."""

    def test_importing_phases_does_not_trigger_github_imports(self) -> None:
        """Importing devloop.phases must not import devloop.github."""
        import sys

        before = "devloop.github" in sys.modules
        from devloop.phases import Phase  # noqa: F401

        after = "devloop.github" in sys.modules
        assert before is after

    def test_importing_execution_does_not_trigger_github_imports(self) -> None:
        """Importing devloop.execution must not import devloop.github."""
        import sys

        before = "devloop.github" in sys.modules
        from devloop.execution import TaskSpec  # noqa: F401

        after = "devloop.github" in sys.modules
        assert before is after

    def test_importing_cichecks_does_not_trigger_github_imports(self) -> None:
        """Importing devloop.cichecks must not import devloop.github."""
        import sys

        before = "devloop.github" in sys.modules
        from devloop.cichecks import CICheckFailure  # noqa: F401

        after = "devloop.github" in sys.modules
        assert before is after


class TestAsInt:
    """Tests for the consolidated as_int helper in devloop.shared."""

    def test_as_int_int_input(self) -> None:
        """Passing an int should return it unchanged."""
        from devloop.shared import as_int

        assert as_int(42) == 42

    def test_as_int_float_string(self) -> None:
        """A float-string cannot be parsed by int() — returns 0."""
        from devloop.shared import as_int

        assert as_int("3.14") == 0

    def test_as_int_none(self) -> None:
        """Passing None should return 0."""
        from devloop.shared import as_int

        assert as_int(None) == 0

    def test_as_int_negative(self) -> None:
        """Negative numbers should round-trip correctly."""
        from devloop.shared import as_int

        assert as_int(-7) == -7

    def test_as_int_zero(self) -> None:
        """Zero should return zero."""
        from devloop.shared import as_int

        assert as_int(0) == 0

    def test_as_int_empty_string(self) -> None:
        """An empty string should return 0 (ValueError catch)."""
        from devloop.shared import as_int

        assert as_int("") == 0
