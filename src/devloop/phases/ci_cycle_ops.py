"""CICycleOps — focused callback protocol for the CI Fix Cycle.

Exposes exactly five fields that the CI fix cycle actually needs:

- ``comment`` — post GitHub PR comments (queued, result).
- ``dispatch_fix`` — dispatch a CI fix agent job (returns commit count).
- ``poll_ci`` — poll CI checks for a PR.
- ``kpi_bump`` — increment per-issue KPI counters (ci_fix_iterations).
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
    _DispatchFixCallback,
    _KpiBumpCallback,
    _PollCiCallback,
    _PostCommentCallback,
)


@dataclass
class CICycleOps:
    """Focused callback protocol for the CI Fix Cycle.

    Each field is ``None`` by default.  When set, the cycle calls the
    callback directly instead of hitting a Temporal activity.
    """

    #: Post a GitHub PR comment.
    comment: _PostCommentCallback | None = None

    #: Dispatch a CI fix agent job.  Returns the commit count.
    dispatch_fix: _DispatchFixCallback | None = None

    #: Poll CI checks for a PR.
    poll_ci: _PollCiCallback | None = None

    #: Increment a per-issue KPI counter.
    kpi_bump: _KpiBumpCallback | None = None

    #: Delete the output ConfigMap for a completed job.
    cleanup: _CleanupCallback | None = None

    @classmethod
    def default(cls) -> "CICycleOps":
        """Return an instance with all fields set to ``None``."""
        return cls()
