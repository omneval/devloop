"""Slack messaging bridge (issue #29).

Provides a Slack-specific ``BotClient`` (slack-bolt Socket Mode client) and
``SlackActivities`` (Temporal activity wrapper) that implement the core
``MessagingPlatform`` protocol and inherit from ``MessagingActivities``.

Usage:
    from devloop.messaging.slack_bot import create_bot, SlackActivities

    bot, handler = create_bot(bot_token, app_token, temporal_client)
    activities = SlackActivities(bot)
"""

from __future__ import annotations

import asyncio
import logging
import os

from slack_bolt import App as SlackApp
from slack_bolt.adapter.socket_mode import SocketModeHandler
from temporalio import activity
from temporalio.client import Client

from devloop.messaging.core import (
    ArchiveThreadInput,
    MessagingActivities,
    SendMessageInput,
    SendMessageOutput,
    SendNotificationInput,
)
from devloop.messaging.text_utils import clamp
from devloop.messaging.thread_store import ConfigMapThreadStore

log = logging.getLogger(__name__)

# Slack platform limits
MAX_MESSAGE = 4000
MAX_THREAD_TOPIC = 100
TRUNC_MARKER = "…"

# Channel name → env key mapping
_CHANNEL_ENV_MAP: dict[str, str] = {
    "approvals": "SLACK_CHANNEL_APPROVALS",
    "alerts": "SLACK_CHANNEL_ALERTS",
    "changelog": "SLACK_CHANNEL_CHANGELOG",
}


def _resolve_channel_ids() -> dict[str, str]:
    """Read Slack channel IDs/names from environment variables."""
    ids: dict[str, str] = {}
    for name, env_key in _CHANNEL_ENV_MAP.items():
        raw = os.getenv(env_key, "")
        if raw:
            ids[name] = raw
    return ids


_CHANNEL_IDS = _resolve_channel_ids()


def channel_id(name: str) -> str:
    """Resolve a logical channel name to its Slack channel ID."""
    cid = _CHANNEL_IDS.get(name)
    if cid is None:
        raise ValueError(
            f"channel '{name}' not configured — set SLACK_CHANNEL_{name.upper()}"
        )
    return cid


# Composite separator for storing channel:thread_ts pairs
_COMPOSITE_SEP = ":"


def _parse_composite(thread_id: str) -> tuple[str, str]:
    """Split ``channel:thread_ts`` back into its components."""
    parts = thread_id.split(_COMPOSITE_SEP, 1)
    if len(parts) != 2:
        raise ValueError(f"invalid thread_id: {thread_id!r}")
    return parts[0], parts[1]


# --------------------------------------------------------------------------- #
# Shared thread store instance
# --------------------------------------------------------------------------- #

_thread_store = ConfigMapThreadStore(configmap_name="slack-thread-map")


# --------------------------------------------------------------------------- #
# BotClient — Slack Socket Mode client
# --------------------------------------------------------------------------- #


class BotClient:
    """Slack bot client that wraps the bolt app and exposes platform methods.

    Implements ``devloop.messaging.MessagingPlatform``.
    """

    def __init__(self, app: SlackApp, temporal_client: Client) -> None:
        self._app = app
        self._temporal = temporal_client

    # ------------------------------------------------------------------
    # MessagingPlatform protocol (synchronous — Slack WebClient is sync)
    # ------------------------------------------------------------------

    def open_thread(
        self,
        channel_name: str,
        thread_name: str,
        initial_message: str,
    ) -> str:
        """Open a thread in *channel_name* by posting a top-level message
        and returning a composite ``channel:thread_ts`` identifier.
        """
        cid = channel_id(channel_name)
        client = self._app.client
        msg = clamp(initial_message, MAX_MESSAGE, TRUNC_MARKER)
        result = client.chat_postMessage(channel=cid, text=msg)
        thread_ts = result["ts"]
        # Post the thread name as a reply so it shows up as the thread topic
        client.chat_postMessage(
            channel=cid,
            thread_ts=thread_ts,
            text=clamp(thread_name, MAX_THREAD_TOPIC),
        )
        log.info("opened thread %s in channel %s", thread_ts, cid)
        # Store as composite so post_to_thread can reconstruct channel + ts
        return f"{cid}{_COMPOSITE_SEP}{thread_ts}"

    def post_to_thread(self, thread_id: str, message: str) -> None:
        """Post a reply to an existing thread."""
        channel, thread_ts = _parse_composite(thread_id)
        client = self._app.client
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=clamp(message, MAX_MESSAGE, TRUNC_MARKER),
        )
        log.info("posted to thread %s", thread_id)

    def archive_thread(self, thread_id: str) -> None:
        """Signal thread closure. Slack doesn't support true thread archiving,
        so we record the closure and remove the mapping.
        """
        log.info("archived (closed) thread %s", thread_id)

    # ------------------------------------------------------------------
    # Signal routing (called by bolt event handlers)
    # ------------------------------------------------------------------

    def get_workflow_handle(self, workflow_id: str):
        return self._temporal.get_workflow_handle(workflow_id)

    def register_event_handlers(self) -> None:
        """Register the bolt ``message`` handler that routes replies."""

        @self._app.message()
        def handle_message(message):
            """Route a Slack thread reply back to the Temporal workflow."""
            thread_ts = message.get("thread_ts")
            if not thread_ts:
                return

            workflow_id = _thread_store.get_workflow(thread_ts)
            if not workflow_id:
                return

            reply_text = message.get("text", "")
            log.info(
                "routing reply from %s in thread %s → workflow %s",
                message.get("user", "unknown"),
                thread_ts,
                workflow_id,
            )
            handle = self.get_workflow_handle(workflow_id)
            loop = asyncio.new_event_loop()
            loop.run_until_complete(handle.signal("human_reply", reply_text))
            loop.close()


def create_bot(
    slack_bot_token: str,
    slack_app_token: str,
    temporal_client: Client,
) -> tuple[BotClient, SocketModeHandler]:
    """Create a Slack bot and return (BotClient, SocketModeHandler).

    Registers event handlers on the bot internally.
    """
    app = SlackApp(token=slack_bot_token)
    bot = BotClient(app, temporal_client)
    bot.register_event_handlers()
    handler = SocketModeHandler(app, slack_app_token)
    return bot, handler


def create_bot_with_app(
    slack_bot_token: str,
    slack_app_token: str,
    temporal_client: Client,
) -> tuple[BotClient, SlackApp, SocketModeHandler]:
    """Create a Slack bot and return BotClient, raw bolt app, and handler.

    Use this variant when you need access to the underlying bolt app.
    """
    bot, handler = create_bot(slack_bot_token, slack_app_token, temporal_client)
    return bot, bot._app, handler


# --------------------------------------------------------------------------- #
# SlackActivities — Temporal activity wrapper
# --------------------------------------------------------------------------- #


class SlackActivities:
    """Wraps a ``BotClient`` as Temporal-compatible activities.

    Inherits the data contract types from the core ``MessagingActivities``
    wrapper but uses Slack-specific I/O.
    """

    def __init__(self, bot: BotClient) -> None:
        self._messaging = MessagingActivities(platform=bot)

    @activity.defn(name="send_message")
    def send_message(self, inp: SendMessageInput) -> SendMessageOutput:
        """Open (or reuse) a Slack thread, post a message, and store the mapping."""
        return self._messaging.send_message_sync(inp)

    @activity.defn(name="send_notification")
    def send_notification(self, inp: SendNotificationInput) -> None:
        """Post a message to the workflow's thread without expecting a reply."""
        self._messaging.send_notification_sync(inp)

    @activity.defn(name="archive_thread")
    def archive_thread(self, inp: ArchiveThreadInput) -> None:
        """Close the Slack thread for a completed workflow."""
        self._messaging.archive_thread_sync(inp)
