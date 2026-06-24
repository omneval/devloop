"""Shared constants — the Agent Job ↔ worker protocol contract.

This is a **deep** module: its interface is a small, stable set of
constants that both the Temporal worker and the agent-base image depend on.

* ``KEY_RESULT`` — ConfigMap key for the ``AgentJobResult`` payload.
* ``KEY_HUMAN_ANSWER`` — ConfigMap key for a human mid-run reply.
* ``ORCHESTRATION_QUEUE`` — Temporal task queue for orchestration workflows.
* ``JOB_DISPATCH_QUEUE`` — Dedicated queue for Agent Execution Job dispatches.

Types such as ``TaskSpec``, ``Phase``, ``CreateGithubIssueInput`` etc. live in
their own deep sub-modules (``devloop.execution``, ``devloop.phases``,
``devloop.github``, ``devloop.cichecks``) and must be imported directly from
there — never via this module.
"""

from __future__ import annotations

import os

ORCHESTRATION_QUEUE: str = os.getenv("ORCHESTRATION_QUEUE", "devloop-orchestration")
JOB_DISPATCH_QUEUE: str = os.getenv("JOB_DISPATCH_QUEUE", "devloop-job-dispatch")
KEY_RESULT: str = "result"
KEY_HUMAN_ANSWER: str = "human_answer"

__all__ = [
    "ORCHESTRATION_QUEUE",
    "JOB_DISPATCH_QUEUE",
    "KEY_RESULT",
    "KEY_HUMAN_ANSWER",
]
