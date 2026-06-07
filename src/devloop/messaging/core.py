"""Messaging platform abstraction and Temporal activity wrapper (issue #19).

Defines the ``MessagingPlatform`` protocol that any messaging bridge (Discord,
Slack, etc.) must implement, the ``MessagingActivities`` generic Temporal
activity wrapper, and shared data contracts.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class SendMessageInput:
    workflow_id: str
    message: str
    channel: str = "approvals"
    thread_name: str = ""


@dataclass
class SendMessageOutput:
    thread_id: str


@dataclass
class SendNotificationInput:
    workflow_id: str
    message: str


@dataclass
class ArchiveThreadInput:
    workflow_id: str


# --------------------------------------------------------------------------- #
# MessagingPlatform protocol
# --------------------------------------------------------------------------- #


@runtime_checkable
class MessagingPlatform(Protocol):
    """Interface every messaging bridge must implement.

    A concrete implementation is responsible for:

    * Translating ``channel_name`` to the platform-specific concept of a
      channel / room / server.
    * Persisting thread mappings so that subsequent calls can reuse an
      existing thread.
    * Respecting platform-specific message size limits.
    """

    def open_thread(
        self,
        channel_name: str,
        thread_name: str,
        initial_message: str,
    ) -> str:
        """Open (or reuse) a thread and post *initial_message*.

        Returns an opaque ``thread_id`` that can be used by
        ``post_to_thread`` and ``archive_thread``.
        """
        ...

    def post_to_thread(self, thread_id: str, message: str) -> None:
        """Post *message* to an existing *thread_id*."""
        ...

    def archive_thread(self, thread_id: str) -> None:
        """Archive / lock *thread_id* so it is no longer active."""
        ...


# --------------------------------------------------------------------------- #
# StubPlatform — test double satisfying MessagingPlatform
# --------------------------------------------------------------------------- #


@dataclass
class StubPlatform:
    """Minimal MessagingPlatform implementation for testing.

    Records every call in ``self.calls`` and returns deterministic
    thread IDs (``stub-<uuid>``) so callers can verify conformance
    without a live platform.
    """

    calls: dict[str, list[dict[str, Any]]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = {
                "open_thread": [],
                "post_to_thread": [],
                "archive_thread": [],
            }

    def open_thread(
        self,
        channel_name: str,
        thread_name: str,
        initial_message: str,
    ) -> str:
        self.calls["open_thread"].append(
            {
                "channel_name": channel_name,
                "thread_name": thread_name,
                "initial_message": initial_message,
            }
        )
        return f"stub-{uuid.uuid4().hex[:8]}"

    def post_to_thread(self, thread_id: str, message: str) -> None:
        self.calls["post_to_thread"].append(
            {"thread_id": thread_id, "message": message}
        )

    def archive_thread(self, thread_id: str) -> None:
        self.calls["archive_thread"].append({"thread_id": thread_id})


# --------------------------------------------------------------------------- #
# MessagingActivities — generic activity wrapper
# --------------------------------------------------------------------------- #


class MessagingActivities:
    """Wraps any ``MessagingPlatform`` to expose Temporal-compatible
    activity methods.

    Exposes three public activity names:

    * ``send_message`` — open/reuse a thread, post a message (expects reply).
    * ``send_notification`` — post to an existing thread (one-way).
    * ``archive_thread`` — lock/archive the workflow thread.

    The ``*_sync`` methods are the synchronous implementations used by
    both Temporal activity decorators and tests.
    """

    def __init__(self, platform: MessagingPlatform) -> None:
        self.platform = platform
        # Map workflow_id -> thread_id so send_notification can reuse threads
        self._thread_map: dict[str, str] = {}
        self._lock = threading.Lock()

        # Public activity aliases (exposed for hasattr / Temporal decorators)
        self.send_message = self.send_message_sync
        self.send_notification = self.send_notification_sync
        self.archive_thread = self.archive_thread_sync

    # -- send_message --

    def send_message_sync(self, inp: SendMessageInput) -> SendMessageOutput:
        with self._lock:
            if inp.workflow_id not in self._thread_map:
                thread_id = self.platform.open_thread(
                    channel_name=inp.channel,
                    thread_name=inp.thread_name,
                    initial_message=inp.message,
                )
                self._thread_map[inp.workflow_id] = thread_id
            else:
                self.platform.post_to_thread(
                    self._thread_map[inp.workflow_id], inp.message
                )
                thread_id = self._thread_map[inp.workflow_id]
        return SendMessageOutput(thread_id=thread_id)

    # -- send_notification --

    def send_notification_sync(self, inp: SendNotificationInput) -> None:
        thread_id = self._thread_map.get(inp.workflow_id)
        if thread_id:
            self.platform.post_to_thread(thread_id, inp.message)
        else:
            # Fallback: open a new thread for notifications with no prior thread
            thread_id = self.platform.open_thread(
                channel_name="alerts",
                thread_name=inp.workflow_id,
                initial_message=inp.message,
            )
            self._thread_map[inp.workflow_id] = thread_id

    # -- archive_thread --

    def archive_thread_sync(self, inp: ArchiveThreadInput) -> None:
        thread_id = self._thread_map.get(inp.workflow_id)
        if thread_id:
            self.platform.archive_thread(thread_id)
