"""Tests for consolidated ``devloop.shared`` — constants only.

After the consolidation that removed type re-exports from sub-modules,
``devloop.shared`` is a deep module that owns exactly four constants.
All other types live in their deep sub-modules and are imported directly.

This is a vertical slice of issue #223: consolidating shared.py re-exports
to reduce interface depth.
"""

from __future__ import annotations


class TestSharedExportsConstantsOnly:
    """shared.py re-exports exactly four constants and no sub-module types."""

    def test_key_result(self) -> None:
        from devloop.shared import KEY_RESULT

        assert KEY_RESULT == "result"

    def test_key_human_answer(self) -> None:
        from devloop.shared import KEY_HUMAN_ANSWER

        assert KEY_HUMAN_ANSWER == "human_answer"

    def test_orchestration_queue(self) -> None:
        from devloop.shared import ORCHESTRATION_QUEUE

        assert isinstance(ORCHESTRATION_QUEUE, str)
        assert len(ORCHESTRATION_QUEUE) > 0

    def test_job_dispatch_queue(self) -> None:
        from devloop.shared import JOB_DISPATCH_QUEUE

        assert isinstance(JOB_DISPATCH_QUEUE, str)
        assert len(JOB_DISPATCH_QUEUE) > 0

    def test_no_type_re_exports(self) -> None:
        """shared.py must not re-export types from sub-modules.

        Types like TaskSpec, Phase, CreateGithubIssueInput etc. are available
        through their own sub-modules (devloop.execution, devloop.phases,
        devloop.github, devloop.cichecks) and through the top-level package
        ``__init__.py``.  Keeping them in shared.py creates a shallow pass-through
        that increases import coupling without adding leverage.
        """
        import devloop.shared

        # These type re-exports must NOT exist in shared.py any more.
        no_types = [
            "TaskSpec",
            "AgentJobResult",
            "DispatchInput",
            "OpenAgentPRsInput",
            "AnswerInput",
            "AwaitInput",
            "PollPRChecksInput",
            "WorkflowKpiInput",
            "Phase",
            "JobStatus",
            "InlineComment",
            "PostCommentsInput",
            "GithubNotificationInput",
            "RequestReviewerInput",
            "ReviewerRequestResult",
            "GetPRBranchInput",
            "GetPRDiffInput",
            "CreateGithubIssueInput",
            "UpdateGithubIssueInput",
            "PublishSummaryInput",
            "PlanIssueInput",
            "CICheckFailure",
            "CIChecksResult",
            "PollCIChecksInput",
        ]
        for name in no_types:
            assert not hasattr(devloop.shared, name), (
                f"devloop.shared.{name} is a type re-export that should have been"
                f" removed; import {name} from its deep sub-module instead."
            )

    def test_exports_list_is_minimal(self) -> None:
        """__all__ should contain only the four constants."""
        import devloop.shared

        expected = {
            "ORCHESTRATION_QUEUE",
            "JOB_DISPATCH_QUEUE",
            "KEY_RESULT",
            "KEY_HUMAN_ANSWER",
        }
        assert set(devloop.shared.__all__) == expected