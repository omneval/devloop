"""Alert Response Workflow — custom consumer example.

This Temporal workflow shows how to extend omneval-devloop with a custom
workflow that responds to AlertManager alerts.  The pattern is:

    1. AlertManager fires → webhook starts AlertResponseWorkflow
    2. A diagnosis Agent Job runs to understand the alert
    3. Each suggested remediation is checked against an allowlist
    4. Allowlisted actions execute autonomously
    5. Non-allowlisted actions pause for human approval via Discord
    6. After remediation, a summary notification is sent

See README.md for the full consumer extension pattern and how to adapt this
example to your own custom workflow.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta

import yaml
from temporalio import workflow
from temporalio.common import RetryPolicy

from devloop import DevLoopInput  # noqa: F401 — shows SDK import works
from devloop.shared import (
    CHANNEL_ALERTS,
    MESSAGING_QUEUE,
    AgentJobResult,
    AnswerInput,
    AwaitInput,
    DispatchInput,
    JobStatus,
    TaskSpec,
)

# --------------------------------------------------------------------------- #
# Allowlist
# --------------------------------------------------------------------------- #

_ALLOWLIST_PATH = "/etc/alert-response/allowlist.yaml"

logger = logging.getLogger(__name__)

_RETRY = RetryPolicy(maximum_attempts=3)
_ACTIVITY_TIMEOUT = timedelta(hours=1)
_DISCORD_TIMEOUT = timedelta(seconds=60)


@dataclass
class AlertResponseInput:
    """Input for the AlertResponseWorkflow."""

    alert_name: str
    alert_labels: dict = field(default_factory=dict)
    alert_annotations: dict = field(default_factory=dict)
    # Which project's Agent Job image to use for diagnosis/remediation.
    # The workflow passes image_override so no Project Registry entry is needed.
    agent_image: str = ""
    # Omneval ingest secret name for the Agent Job.
    omneval_secret: str = ""
    # Service account the Agent Job pod runs as.
    service_account: str = ""
    # Timeout for human approval gates (seconds). 0 = wait forever.
    approval_timeout_seconds: float = 3600.0


def load_allowlist(path: str = _ALLOWLIST_PATH) -> dict:
    """Load the action allowlist from YAML.

    Returns a dict mapping action categories to lists of allowed actions, e.g.

    .. code-block:: yaml

        restart:
          - nginx
          - redis
        scale:
          - web-frontend

    Actions not in the allowlist require human approval.
    """
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        logger.info("loaded allowlist from %s: %d categories", path, len(data))
        return data
    except FileNotFoundError:
        logger.warning("allowlist not found at %s — all actions require approval", path)
        return {}


def _is_allowlisted(action: str, category: str, allowlist: dict) -> bool:
    """Check whether an action is pre-approved by the allowlist."""
    allowed = allowlist.get(category, [])
    return action in allowed


# --------------------------------------------------------------------------- #
# Workflow
# --------------------------------------------------------------------------- #


@workflow.defn
class AlertResponseWorkflow:
    """Handle an alert end-to-end: diagnose → remediate → notify.

    Allowlisted actions run autonomously; everything else pauses for a human
    reply on Discord.
    """

    def __init__(self) -> None:
        self._replies: list[str] = []
        self._consumed = 0
        self._ask_lock: asyncio.Lock | None = None

    # ---- signals ---------------------------------------------------------- #

    @workflow.signal
    def human_reply(self, text: str) -> None:
        """Signal a human approval / rejection for a non-allowlisted action."""
        self._replies.append(text)

    # ---- helpers ---------------------------------------------------------- #

    def _workflow_id(self) -> str:
        return workflow.info().workflow_id

    async def _send_message(
        self, message: str, thread_name: str = "", channel: str = CHANNEL_ALERTS
    ) -> None:
        with workflow.unsafe.import_outside_workflow_sandbox_mode():
            from devloop.shared import SendMessageInput

        await workflow.execute_activity(
            "send_message",
            SendMessageInput(self._workflow_id(), message, channel, thread_name),
            task_queue=MESSAGING_QUEUE,
            start_to_close_timeout=_DISCORD_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _notify(self, message: str) -> None:
        with workflow.unsafe.import_outside_workflow_sandbox_mode():
            from devloop.shared import SendNotificationInput

        await workflow.execute_activity(
            "send_notification",
            SendNotificationInput(self._workflow_id(), message),
            task_queue=MESSAGING_QUEUE,
            start_to_close_timeout=_DISCORD_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _await_reply(self, timeout: float | None = None) -> str | None:
        """Block until the next unconsumed human reply; None on timeout."""
        target = self._consumed + 1
        try:
            await workflow.wait_condition(
                lambda: len(self._replies) >= target,
                timeout=timedelta(seconds=timeout) if timeout else None,
            )
        except asyncio.TimeoutError:
            return None
        reply = self._replies[self._consumed]
        self._consumed += 1
        return reply

    async def _dispatch_diagnosis(
        self, inp: AlertResponseInput
    ) -> AgentJobResult:
        """Run the diagnosis Agent Job and return its result."""
        return await workflow.execute_activity(
            "dispatch_agent_job",
            DispatchInput(
                project_id=inp.alert_name,
                issue_number=0,
                task_spec=TaskSpec(
                    phase="diagnosis",
                    project_id=inp.alert_name,
                    extra={
                        "alert_labels": inp.alert_labels,
                        "alert_annotations": inp.alert_annotations,
                    },
                ),
                image_override=inp.agent_image,
                omneval_secret_override=inp.omneval_secret,
                service_account_override=inp.service_account,
            ),
            result_type=AgentJobResult,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _dispatch_remediation(
        self, inp: AlertResponseInput, action: str, category: str
    ) -> AgentJobResult:
        """Run a remediation Agent Job for a single allowed action."""
        return await workflow.execute_activity(
            "dispatch_agent_job",
            DispatchInput(
                project_id=inp.alert_name,
                issue_number=0,
                task_spec=TaskSpec(
                    phase="remediation",
                    project_id=inp.alert_name,
                    extra={
                        "action": action,
                        "category": category,
                        "alert_labels": inp.alert_labels,
                    },
                ),
                image_override=inp.agent_image,
                omneval_secret_override=inp.omneval_secret,
                service_account_override=inp.service_account,
            ),
            result_type=AgentJobResult,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

    async def _answer_and_await(
        self, inp: AlertResponseInput, result: AgentJobResult
    ) -> AgentJobResult:
        """Handle an AWAITING_HUMAN result: ask Discord, then resume polling."""
        while result.status == JobStatus.AWAITING_HUMAN.value:
            async with self._ask_lock:
                await self._send_message(f"❓ [{inp.alert_name}] {result.question}")
                answer = await self._await_reply(
                    timeout=inp.approval_timeout_seconds
                    if inp.approval_timeout_seconds > 0
                    else None
                )
            if answer is None:
                answer = (
                    "No human reply within the timeout — proceed with best guess."
                )
                await self._notify(
                    f"⏱️ [{inp.alert_name}] no reply — proceeding with best guess."
                )
            await workflow.execute_activity(
                "answer_agent_job",
                AnswerInput(result.job_name, answer),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=_RETRY,
            )
            result = await workflow.execute_activity(
                "await_agent_job",
                AwaitInput(
                    result.job_name,
                    poll_interval_seconds=5.0,
                ),
                result_type=AgentJobResult,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_RETRY,
            )
        return result

    # ---- run -------------------------------------------------------------- #

    @workflow.run
    async def run(self, inp: AlertResponseInput) -> str:
        """Execute the full alert response lifecycle.

        Returns a summary string describing what happened.
        """
        self._ask_lock = asyncio.Lock()
        thread_name = f"Alert: {inp.alert_name}"
        allowlist = workflow.query(load_allowlist, path=_ALLOWLIST_PATH)

        steps: list[str] = []

        # 1. Diagnose
        steps.append(f"🔍 Diagnosing alert: {inp.alert_name}")
        await self._send_message(steps[-1], thread_name=thread_name)
        diagnosis = await self._dispatch_diagnosis(inp)
        diagnosis = await self._answer_and_await(inp, diagnosis)

        if diagnosis.status != JobStatus.COMPLETE.value:
            await self._notify(
                f"❌ [{inp.alert_name}] diagnosis failed: {diagnosis.error or diagnosis.summary}"
            )
            return f"diagnosis_failed: {diagnosis.error}"

        diag_data = diagnosis.diagnosis or {}
        suggested_actions = diag_data.get("actions", [])
        if not suggested_actions:
            await self._notify(
                f"✅ [{inp.alert_name}] diagnosis complete — no actions needed."
            )
            return f"diagnosed: {diagnosis.summary}"

        # 2. Remediate each suggested action
        for action_spec in suggested_actions:
            action = action_spec.get("action", "")
            category = action_spec.get("category", "other")

            if _is_allowlisted(action, category, allowlist):
                steps.append(f"✅ [{action}] allowlisted — executing")
                await self._send_message(steps[-1], thread_name=thread_name)
            else:
                steps.append(f"⚠️ [{action}] not allowlisted — requesting approval")
                await self._send_message(steps[-1], thread_name=thread_name)
                reply = await self._await_reply(
                    timeout=inp.approval_timeout_seconds
                    if inp.approval_timeout_seconds > 0
                    else None
                )
                if reply is None or not reply.lower().startswith("approve"):
                    steps.append(f"⏭️ [{action}] skipped (not approved)")
                    await self._notify(
                        f"⏭️ [{inp.alert_name}] {action} not approved — skipping."
                    )
                    continue

            rem_result = await self._dispatch_remediation(inp, action, category)
            rem_result = await self._answer_and_await(inp, rem_result)

            if rem_result.status == JobStatus.COMPLETE.value:
                steps.append(f"🟢 [{action}] completed")
            else:
                steps.append(f"🔴 [{action}] failed: {rem_result.error}")

        # 3. Summary
        summary = "\n".join(steps)
        await self._notify(f"📋 [{inp.alert_name}] Response complete:\n{summary}")
        return summary
