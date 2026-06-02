"""Slack Socket Mode client.

Uses slack-bolt's Socket Mode adapter (no public inbound port required) to
receive messages in managed threads and forward replies to the appropriate
Temporal workflow as a ``human_reply`` signal.

Implements the ``devloop.messaging.MessagingPlatform`` protocol so the same
generic ``MessagingActivities`` wrapper can be reused.
"""

import logging
import os

from slack_bolt import App as SlackApp
from slack_bolt.adapter.socket_mode import SocketModeHandler
from temporalio.client import Client

import thread_store
from text_utils import MAX_MESSAGE, MAX_THREAD_TOPIC, TRUNC_MARKER, clamp

log = logging.getLogger(__name__)

_CHANNEL_IDS: dict[str, str] = {}

# Composite separator for storing channel:thread_ts pairs
_COMPOSITE_SEP = ":"


def _resolve_channels() -> None:
    for name, env_key in (
        ("approvals", "SLACK_CHANNEL_APPROVALS"),
        ("alerts", "SLACK_CHANNEL_ALERTS"),
        ("changelog", "SLACK_CHANNEL_CHANGELOG"),
    ):
        raw = os.getenv(env_key, "")
        if raw:
            _CHANNEL_IDS[name] = raw


_resolve_channels()


def channel_id(name: str) -> str:
    cid = _CHANNEL_IDS.get(name)
    if cid is None:
        raise ValueError(
            f"channel '{name}' not configured — set SLACK_CHANNEL_{name.upper()}"
        )
    return cid


def _parse_composite(thread_id: str) -> tuple[str, str]:
    """Split ``channel:thread_ts`` back into its components."""
    parts = thread_id.split(_COMPOSITE_SEP, 1)
    if len(parts) != 2:
        raise ValueError(f"invalid thread_id: {thread_id!r}")
    return parts[0], parts[1]


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

            workflow_id = thread_store.get_workflow(thread_ts)
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
            import asyncio

            loop = asyncio.new_event_loop()
            loop.run_until_complete(handle.signal("human_reply", reply_text))
            loop.close()


def create_bot(
    slack_bot_token: str,
    slack_app_token: str,
    temporal_client: Client,
) -> tuple[BotClient, SlackApp, SocketModeHandler]:
    """Create and configure a Slack bot with Socket Mode.

    Returns the BotClient, the raw bolt app, and the SocketModeHandler.
    """
    app = SlackApp(token=slack_bot_token)
    bot = BotClient(app, temporal_client)
    bot.register_event_handlers()
    handler = SocketModeHandler(app, slack_app_token)
    return bot, app, handler
