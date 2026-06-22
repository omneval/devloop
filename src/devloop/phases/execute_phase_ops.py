"""ExecutePhaseOps — focused callback protocol for the Execute phase.

Exposes exactly four fields that the Execute phase actually needs:

- ``comment`` — post GitHub Issue/PR comments (queued, implemented, exhausted).
- ``dispatch_execute`` — dispatch the execute agent job.
- ``answer_question`` — resolve mid-run AWAITING_HUMAN questions.
- ``kpi_bump`` — increment per-issue KPI counters (execute_attempts).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Coroutine

if TYPE_CHECKING:
    from ..execution import AgentJobResult, TaskSpec


# Type alias: post a GitHub Issue/PR comment.
_PostCommentCallback = Callable[[str, int, str], Coroutine[Any, Any, None]]

# Type alias: dispatch the execute agent job and wait for result.
_DispatchExecuteCallback = Callable[
    [str, "TaskSpec", int, float], Coroutine[Any, Any, "AgentJobResult"]
]

# Type alias: resolve an AWAITING_HUMAN question for an execute job.
_AnswerQuestionCallback = Callable[
    [str, int, "AgentJobResult"], Coroutine[Any, Any, "AgentJobResult"]
]

# Type alias: increment a per-issue KPI counter.
_KpiBumpCallback = Callable[[str, int], Coroutine[Any, Any, None]]


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
