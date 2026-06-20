import os
from datetime import timedelta

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
