"""ExecutePhaseOps — focused callback protocol for the Execute phase.

Exposes exactly four fields that the Execute phase actually needs:

- ``comment`` — post GitHub Issue/PR comments (queued, implemented, exhausted).
- ``dispatch_execute`` — dispatch the execute agent job.
- ``answer_question`` — resolve mid-run AWAITING_HUMAN questions.
- ``kpi_bump`` — increment per-issue KPI counters (execute_attempts).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# Re-export shared callback types from _types.py
from ._types import (  # noqa: E401
    _AnswerQuestionCallback,
    _DispatchExecuteCallback,
    _KpiBumpCallback,
    _PostCommentCallback,
)


@dataclass
class ExecutePhaseOps:
    """Focused callback protocol for the Execute phase.

    Each field is ``None`` by default.  When set, the phase calls the
    callback directly instead of hitting a Temporal activity.
    """

    #: Post a GitHub Issue/PR comment.
    comment: _PostCommentCallback | None = None

    #: Dispatch the execute agent job.
    dispatch_execute: _DispatchExecuteCallback | None = None

    #: Resolve an ``AWAITING_HUMAN`` question for an execute job.
    answer_question: _AnswerQuestionCallback | None = None

    #: Increment a per-issue KPI counter.
    kpi_bump: _KpiBumpCallback | None = None

    @classmethod
    def default(cls) -> "ExecutePhaseOps":
        """Return an instance with all fields set to ``None``."""
        return cls()
