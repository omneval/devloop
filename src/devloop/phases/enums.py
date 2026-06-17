"""Phase and JobStatus enums for the workflow phase pipeline.

These enums are used throughout the Dev Loop workflow orchestration —
each phase maps to an agent execution job and its bundled prompt template.
"""

from __future__ import annotations

from enum import Enum


class Phase(str, Enum):
    """Dev Loop phases — each is an agent execution job + prompt template."""

    PLAN = "plan"
    EXECUTE = "execute"
    REVIEW = "review"
    DIAGNOSIS = "diagnosis"
    CI_FIX = "ci_fix"
    SUMMARIZE = "summarize"
    ANSWER = "answer"
    PR_COMMENT = "pr_comment"
    CODE_QUALITY_SCAN = "code_quality_scan"
    CODE_QUALITY_IMPROVE = "code_quality_improve"


class JobStatus(str, Enum):
    """Terminal status of an Agent Execution Job."""

    COMPLETE = "complete"
    FAILED = "failed"
    AWAITING_HUMAN = "awaiting_human"
