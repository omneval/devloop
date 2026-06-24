from __future__ import annotations

import os
import re
from datetime import timedelta
from typing import Any

from temporalio.common import RetryPolicy

_RETRY = RetryPolicy(maximum_attempts=3)
_GITHUB_COMMENT_TIMEOUT = timedelta(seconds=60)
_DISPATCH_TIMEOUT = timedelta(seconds=60)

# Temporal activity timeout for Agent Execution Jobs. Must exceed
# AGENT_JOB_ACTIVE_DEADLINE so Temporal always outlasts the K8s job and
# can cleanly detect failure — the 90s buffer covers K8s status propagation.
_ACTIVITY_TIMEOUT = timedelta(
    seconds=int(os.getenv("AGENT_JOB_ACTIVE_DEADLINE", "7200")) + 90
)

JOB_DISPATCH_QUEUE = os.getenv("JOB_DISPATCH_QUEUE", "devloop-job-dispatch")

# Webhook constants — defined once in _constants so all callers import them
# rather than each duplicating their own copy (improves locality).
_AGENT_BRANCH = re.compile(r"^agent/issue-(\d+)")
AGENT_GITHUB_LOGIN: str = os.environ.get("AGENT_GITHUB_LOGIN", "devloop-bot")


def _as_int(value: Any) -> int:
    """Safely convert *value* to ``int``, returning ``0`` on failure."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
