"""ReviewPhaseOps — focused callback protocol for the Review phase.

Exposes exactly four fields that the Review phase actually needs:

- ``comment`` — post GitHub Issue/PR comments (queued, reviewed).
- ``dispatch_review`` — dispatch the review agent job.
- ``post_review_findings`` — post reviewer's findings to the PR.
- ``cleanup`` — delete the output ConfigMap for a completed job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from ..execution import AgentJobResult, TaskSpec


# Type alias: post a GitHub Issue/PR comment.
_PostCommentCallback = Callable[[str, int, str], Coroutine[Any, Any, None]]

# Type alias: dispatch the review agent job and wait for result.
_DispatchReviewCallback = Callable[
    [str, "TaskSpec", int, float], Coroutine[Any, Any, AgentJobResult]
]

# Type alias: post reviewer's findings (summary + inline comments) to the PR.
_PostReviewFindingsCallback = Callable[
    [str, str, dict, AgentJobResult], Coroutine[Any, Any, None]
]

# Type alias: delete the output ConfigMap for a completed job.
_CleanupCallback = Callable[[str], Coroutine[Any, Any, None]]


@dataclass
class ReviewPhaseOps:
    """Focused callback protocol for the Review phase.

    Each field is ``None`` by default.  When set, the phase calls the
    callback directly instead of hitting a Temporal activity.
    """

    #: Post a GitHub Issue/PR comment.
    comment: _PostCommentCallback | None = None

    #: Dispatch the review agent job.
    dispatch_review: _DispatchReviewCallback | None = None

    #: Post the reviewer's findings to the PR (summary + inline comments).
    post_review_findings: _PostReviewFindingsCallback | None = None

    #: Delete the output ConfigMap for a completed job.
    cleanup: _CleanupCallback | None = None

    @classmethod
    def default(cls) -> "ReviewPhaseOps":
        """Return an instance with all fields set to ``None``."""
        return cls()
