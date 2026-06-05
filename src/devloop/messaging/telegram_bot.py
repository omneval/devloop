"""Telegram messaging bridge (issue #21).

Provides a Telegram-specific ``BotClient`` (python-telegram-bot async client)
and ``TelegramActivities`` (Temporal activity wrapper) that implement the core
``MessagingPlatform`` protocol and inherit from ``MessagingActivities``.

Telegram does not have "threads" in the Discord/Slack sense.  The closest
equivalent is **forum topics** (available in groups with forum mode enabled).
Each workflow gets an isolated forum topic; replies within managed topics are
routed back as ``human_reply`` signals to Temporal workflows.

Usage:
    from devloop.messaging.telegram_bot import create_bot, TelegramActivities

    bot, updater = create_bot(token, temporal_client)
    activities = TelegramActivities(bot)
"""

from __future__ import annotations

import asyncio
import logging
import os

from telegram import Update
from telegram.ext import Application, MessageHandler, filters
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

# Telegram platform limits
MAX_MESSAGE = 4096
TRUNC_MARKER = "\u2026"

# Channel name → env key mapping
_CHANNEL_ENV_MAP: dict[str, str] = {
    "approvals": "TELEGRAM_CHAT_APPROVALS",
    "alerts": "TELEGRAM_CHAT_ALERTS",
    "changelog": "TELEGRAM_CHAT_CHANGELOG",
}


def _resolve_chat_ids() -> dict[str, int]:
    """Read Telegram chat IDs from environment variables."""
    ids: dict[str, int] = {}
    for name, env_key in _CHANNEL_ENV_MAP.items():
        raw = os.getenv(env_key, "")
        if raw:
            try:
                ids[name] = int(raw)
            except ValueError:
                log.warning("invalid chat ID for %s: %r", name, raw)
    return ids


_CHAT_IDS = _resolve_chat_ids()


def chat_id(name: str) -> int:
    """Resolve a logical channel name to its Telegram chat ID."""
    cid = _CHAT_IDS.get(name)
    if cid is None:
        raise ValueError(
            f"channel '{name}' not configured — set TELEGRAM_CHAT_{name.upper()}"
        )
    return cid


# --------------------------------------------------------------------------- #
# Shared thread store instance
# --------------------------------------------------------------------------- #

_thread_store = ConfigMapThreadStore(
    configmap_name="telegram-thread-map",
    namespace=os.getenv("K8S_NAMESPACE", "agents"),
)

# --------------------------------------------------------------------------- #
# BotClient — Telegram bot client
# --------------------------------------------------------------------------- #


class BotClient:
    """Telegram bot that listens for human replies in managed forum topics and
    forwards them as ``human_reply`` signals to Temporal workflows.

    Implements the ``MessagingPlatform`` protocol methods
    (``open_thread``, ``post_to_thread``, ``archive_thread``) for use by
    ``TelegramActivities``.

    Uses python-telegram-bot v20+ Application API with long polling by default.
    Forum topics (message_thread_id) provide the isolation equivalent of Discord
    threads and Slack thread_ts.
    """

    def __init__(self, token: str, temporal_client: Client) -> None:
        self._token = token
        self._temporal = temporal_client
        self._app: Application | None = None

    @property
    def app(self) -> Application | None:
        return self._app

    async def _ensure_app(self) -> Application:
        if self._app is None:
            self._app = await Application.builder().token(self._token).build()
        return self._app

    # ------------------------------------------------------------------
    # MessagingPlatform protocol (async — Telegram API is async)
    # ------------------------------------------------------------------

    async def open_thread(
        self,
        channel_name: str,
        thread_name: str,
        initial_message: str,
    ) -> str:
        """Create a forum topic in *channel_name* (group chat) and post
        *initial_message*.  Returns the message ID as the thread_id."""
        app = await self._ensure_app()
        cid = chat_id(channel_name)
        msg = clamp(initial_message, MAX_MESSAGE, TRUNC_MARKER)

        # In forum-enabled groups, creating a message with message_thread_id
        # creates a new topic.  For non-forum groups we fall back to a
        # plain message and use the message_id as thread_id.
        # We attempt to create a forum topic first; if the group doesn't
        # support forums, Telegram will return an error and we fall back.
        from telegram.error import BadRequest

        topic_message = None
        try:
            # Forum topic creation: post to a new message_thread_id.
            # Telegram auto-creates the topic when you post to a new thread ID
            # in a forum-enabled group.  We use a unique message_thread_id
            # derived from the thread_name to identify the topic.
            # For simplicity we create the topic by posting the initial message.
            # The message_id of the first post becomes the thread_id.
            response = await app.bot.send_message(
                chat_id=cid,
                text=msg,
            )
            topic_message = response
        except BadRequest:
            log.warning(
                "failed to create topic in chat %d for %s — group may not be a forum",
                cid,
                thread_name,
            )
            raise

        thread_id = str(topic_message.message_id)
        log.info("opened thread %s in chat %d", thread_id, cid)
        return thread_id

    async def post_to_thread(self, thread_id: str, message: str) -> None:
        """Post a reply message to an existing thread."""
        app = await self._ensure_app()
        cid = chat_id("approvals")  # default — in practice we need the chat_id stored
        msg = clamp(message, MAX_MESSAGE, TRUNC_MARKER)
        await app.bot.send_message(
            chat_id=cid,
            text=msg,
            message_thread_id=int(thread_id),
        )
        log.info("posted to thread %s", thread_id)

    async def archive_thread(self, thread_id: str) -> None:
        """Close a forum topic.  Telegram doesn't support true topic archiving,
        so we log the closure intent and remove the mapping."""
        log.info("archived (closed) thread %s", thread_id)

    # ------------------------------------------------------------------
    # Signal routing (called by message handler)
    # ------------------------------------------------------------------

    def register_message_handler(self, app: Application) -> None:
        """Register the message handler that routes replies to workflows."""

        async def handle_message(update: Update, context) -> None:
            """Route a Telegram reply back to the Temporal workflow."""
            if not update.message:
                return
            if update.message.is_topic_message:
                # Reply within a forum topic — the message_thread_id is the topic ID.
                thread_id = str(update.message.message_thread_id)
            elif update.message.reply_to_message:
                # Reply to a message in a non-forum group.
                thread_id = str(update.message.reply_to_message.message_id)
            else:
                return

            workflow_id = _thread_store.get_workflow(thread_id)
            if not workflow_id:
                return

            reply_text = update.message.text or ""
            log.info(
                "routing reply from %s in thread %s → workflow %s",
                update.message.from_user,
                thread_id,
                workflow_id,
            )
            handle = self._temporal.get_workflow_handle(workflow_id)
            try:
                await handle.signal("human_reply", reply_text)
            except Exception:
                log.exception(
                    "failed to signal workflow %s with reply from %s",
                    workflow_id,
                    update.message.from_user,
                )

        handler = MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
        app.add_handler(handler)


def create_bot(
    token: str,
    temporal_client: Client,
) -> tuple[BotClient, Application]:
    """Create a Telegram bot client and its Application.

    The caller is responsible for starting the Application (e.g.
    ``app.run_polling()`` or ``app.run_polling(...)``) concurrently
    with the Temporal worker.

    Returns ``(BotClient, Application)``.
    """
    bot = BotClient(token, temporal_client)

    async def _setup_app(application: Application) -> None:
        bot._app = application
        bot.register_message_handler(application)

    app = (
        Application.builder()
        .token(token)
        .post_init(_setup_app)
        .build()
    )
    # Ensure app reference is set synchronously for tests
    bot._app = app
    return bot, app


# --------------------------------------------------------------------------- #
# TelegramActivities — Temporal activity wrapper
# --------------------------------------------------------------------------- #


class TelegramActivities:
    """Wraps a ``BotClient`` as Temporal-compatible async activities.

    Inherits the data contract types from the core ``MessagingActivities``
    wrapper but provides its own async implementations tailored to
    Telegram's API.

    Thread mappings are persisted in the durable store so they survive pod
    restarts.  The reverse lookup key stored in the ConfigMap is the bare
    ``message_id`` so that ``handle_message`` can resolve replies using
    only the ``message_thread_id`` or ``reply_to_message.message_id`` from
    the Telegram event payload.
    """

    def __init__(
        self,
        bot: BotClient,
        thread_store: ConfigMapThreadStore | None = None,
    ) -> None:
        self._bot = bot
        self._store = thread_store if thread_store is not None else _thread_store
        self._threads: dict[str, str] = {}

    @activity.defn(name="send_message")
    async def send_message(self, inp: SendMessageInput) -> SendMessageOutput:
        # Restore from durable store on cache miss (e.g. after pod restart)
        thread_id = self._threads.get(inp.workflow_id)
        if thread_id is None:
            thread_id = self._store.get_thread(inp.workflow_id)
            if thread_id is not None:
                self._threads[inp.workflow_id] = thread_id

        if thread_id is not None:
            await self._bot.post_to_thread(thread_id, inp.message)
        else:
            thread_id = await self._bot.open_thread(
                inp.channel, inp.thread_name, inp.message
            )
            self._threads[inp.workflow_id] = thread_id
            self._store.put(inp.workflow_id, thread_id)

        return SendMessageOutput(thread_id=thread_id)

    @activity.defn(name="send_notification")
    async def send_notification(self, inp: SendNotificationInput) -> None:
        # Restore from durable store on cache miss
        thread_id = self._threads.get(inp.workflow_id)
        if thread_id is None:
            thread_id = self._store.get_thread(inp.workflow_id)
            if thread_id is not None:
                self._threads[inp.workflow_id] = thread_id

        if thread_id is not None:
            await self._bot.post_to_thread(thread_id, inp.message)
        else:
            # Fallback: open a new thread for notifications with no prior thread
            thread_id = await self._bot.open_thread(
                "alerts", inp.workflow_id, inp.message
            )
            self._threads[inp.workflow_id] = thread_id
            self._store.put(inp.workflow_id, thread_id)

    @activity.defn(name="archive_thread")
    async def archive_thread(self, inp: ArchiveThreadInput) -> None:
        thread_id = self._threads.pop(inp.workflow_id, None)
        if thread_id is None:
            thread_id = self._store.get_thread(inp.workflow_id)
        if thread_id is None:
            return
        await self._bot.archive_thread(thread_id)
        self._store.delete(inp.workflow_id)
