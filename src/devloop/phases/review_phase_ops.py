"""ReviewPhaseOps — focused callback protocol for the Review phase.

Exposes exactly four fields that the Review phase actually needs:

- ``comment`` — post GitHub Issue/PR comments (queued, reviewed).
- ``dispatch_review`` — dispatch the review agent job.
- ``post_review_findings`` — post reviewer's findings to the PR.
- ``cleanup`` — delete the output ConfigMap for a completed job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    pass

# Re-export shared callback types from _types.py
from ._types import (  # noqa: E401
    _CleanupCallback,
    _DispatchReviewCallback,
    _PostCommentCallback,
    _PostReviewFindingsCallback,
)


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
