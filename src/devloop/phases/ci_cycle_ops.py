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
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from ..cichecks import CIChecksResult

if TYPE_CHECKING:
    from ..execution import TaskSpec


# Type alias: post a GitHub PR comment.
_PostCommentCallback = Callable[[str, int, str], Coroutine[Any, Any, None]]

# Type alias: dispatch a CI fix agent job, returns commit count (int).
_DispatchFixCallback = Callable[[str, "TaskSpec", int, float], Coroutine[Any, Any, int]]

# Type alias: poll CI checks for a PR.
_PollCiCallback = Callable[[str, int], Coroutine[Any, Any, CIChecksResult]]

# Type alias: increment a per-issue KPI counter.
_KpiBumpCallback = Callable[[str, int], Coroutine[Any, Any, None]]

# Type alias: delete the output ConfigMap for a completed job.
_CleanupCallback = Callable[[str], Coroutine[Any, Any, None]]


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
