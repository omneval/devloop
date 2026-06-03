"""Discord messaging bridge (issue #29).

Provides a Discord-specific ``BotClient`` (discord.py gateway client) and
``DiscordActivities`` (Temporal activity wrapper) that inherit from the core
``MessagingPlatform`` protocol and ``MessagingActivities`` wrapper.

Usage:
    from devloop.messaging.discord_bot import BotClient, DiscordActivities

    bot = BotClient(token="...", temporal_client=client)
    activities = DiscordActivities(bot)
"""

from __future__ import annotations

import logging
import os

import discord
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

# Discord platform limits
MAX_MESSAGE = 2000
MAX_THREAD_NAME = 100
TRUNC_MARKER = "\n… [truncated]"

# Channel name → env key mapping
_CHANNEL_ENV_MAP: dict[str, str] = {
    "approvals": "DISCORD_CHANNEL_APPROVALS",
    "alerts": "DISCORD_CHANNEL_ALERTS",
    "changelog": "DISCORD_CHANNEL_CHANGELOG",
}


def _resolve_channel_ids() -> dict[str, int]:
    """Read Discord channel IDs from environment variables."""
    ids: dict[str, int] = {}
    for name, env_key in _CHANNEL_ENV_MAP.items():
        raw = os.getenv(env_key, "")
        if raw:
            try:
                ids[name] = int(raw)
            except ValueError:
                log.warning("invalid channel ID for %s: %r", name, raw)
    return ids


_CHANNEL_IDS = _resolve_channel_ids()


def channel_id(name: str) -> int:
    """Resolve a logical channel name to its Discord numeric ID."""
    cid = _CHANNEL_IDS.get(name)
    if cid is None:
        raise ValueError(
            f"channel '{name}' not configured — set DISCORD_CHANNEL_{name.upper()}"
        )
    return cid


# --------------------------------------------------------------------------- #
# Shared thread store instance
# --------------------------------------------------------------------------- #

_thread_store = ConfigMapThreadStore(configmap_name="discord-bot-threads")


# --------------------------------------------------------------------------- #
# BotClient — Discord gateway client
# --------------------------------------------------------------------------- #


class BotClient(discord.Client):
    """Discord bot that listens for human replies in managed threads and
    forwards them as ``human_reply`` signals to Temporal workflows.

    Also implements the ``MessagingPlatform`` protocol methods
    (``open_thread``, ``post_to_thread``, ``archive_thread``) for use by
    ``DiscordActivities``.
    """

    def __init__(self, token: str, temporal_client: Client) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self._token = token
        self._temporal = temporal_client

    async def on_ready(self) -> None:
        log.info("discord bot ready: %s (id=%s)", self.user, self.user.id)

    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.user:
            return

        if not isinstance(message.channel, discord.Thread):
            return

        thread_id = str(message.channel.id)
        workflow_id = _thread_store.get_workflow(thread_id)
        if not workflow_id:
            return

        log.info(
            "routing reply from %s in thread %s → workflow %s",
            message.author,
            thread_id,
            workflow_id,
        )
        handle = self._temporal.get_workflow_handle(workflow_id)
        await handle.signal("human_reply", message.content)

    # -- MessagingPlatform protocol (async variants) ------------------------

    async def open_thread(
        self,
        channel_name: str,
        thread_name: str,
        initial_message: str,
    ) -> discord.Thread:
        cid = channel_id(channel_name)
        channel: discord.TextChannel = self.get_channel(cid)
        if channel is None:
            channel = await self.fetch_channel(cid)

        thread = await channel.create_thread(
            name=clamp(thread_name, MAX_THREAD_NAME),
            type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,  # 7 days
        )
        await thread.send(clamp(initial_message, MAX_MESSAGE, TRUNC_MARKER))
        return thread

    async def post_to_thread(self, thread_id: str, message: str) -> None:
        thread = self.get_channel(int(thread_id))
        if thread is None:
            thread = await self.fetch_channel(int(thread_id))
        await thread.send(clamp(message, MAX_MESSAGE, TRUNC_MARKER))

    async def archive_thread(self, thread_id: str) -> None:
        thread = self.get_channel(int(thread_id))
        if thread is None:
            thread = await self.fetch_channel(int(thread_id))
        await thread.edit(archived=True, locked=True)
        log.info("archived thread %s", thread_id)


# --------------------------------------------------------------------------- #
# DiscordActivities — Temporal activity wrapper
# --------------------------------------------------------------------------- #


class DiscordActivities:
    """Wraps a ``BotClient`` as Temporal-compatible async activities.

    Uses the same input/output data contracts as ``MessagingActivities`` but
    provides its own async implementations tailored to Discord's API.
    """

    def __init__(self, bot: BotClient) -> None:
        self._bot = bot
        self._threads: dict[str, str] = {}

    async def send_message(self, inp: SendMessageInput) -> SendMessageOutput:
        # Reuse existing thread for this workflow, or open a new one
        thread_id = self._threads.get(inp.workflow_id)
        if thread_id is None:
            thread_id = _thread_store.get_thread(inp.workflow_id)
            self._threads[inp.workflow_id] = thread_id

        if thread_id is not None:
            await self._bot.post_to_thread(thread_id, inp.message)
        else:
            thread = await self._bot.open_thread(
                inp.channel, inp.thread_name, inp.message
            )
            thread_id = str(thread.id)
            self._threads[inp.workflow_id] = thread_id
            _thread_store.put(inp.workflow_id, thread_id)

        return SendMessageOutput(thread_id=thread_id)

    async def send_notification(self, inp: SendNotificationInput) -> None:
        thread_id = self._threads.get(inp.workflow_id)
        if thread_id is None:
            thread_id = _thread_store.get_thread(inp.workflow_id)

        if thread_id is not None:
            await self._bot.post_to_thread(thread_id, inp.message)
        else:
            # Fallback: open a new thread for notifications with no prior thread
            thread = await self._bot.open_thread(
                "alerts", inp.workflow_id, inp.message
            )
            thread_id = str(thread.id)
            self._threads[inp.workflow_id] = thread_id
            _thread_store.put(inp.workflow_id, thread_id)

    async def archive_thread(self, inp: ArchiveThreadInput) -> None:
        thread_id = self._threads.pop(inp.workflow_id, None)
        if thread_id is None:
            thread_id = _thread_store.get_thread(inp.workflow_id)
        if thread_id is None:
            return
        await self._bot.archive_thread(thread_id)
        _thread_store.delete(inp.workflow_id)
