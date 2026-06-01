"""Discord gateway client.

Listens for messages in managed threads and forwards replies to the
appropriate Temporal workflow as a `human_reply` signal.

Required privileged intents (enable in the Discord Developer Portal):
  - Message Content Intent
"""

import logging
import os

import discord
from temporalio.client import Client

import thread_store
from text_utils import MAX_MESSAGE, MAX_THREAD_NAME, TRUNC_MARKER, clamp

log = logging.getLogger(__name__)

_CHANNEL_IDS: dict[str, int] = {}


def _resolve_channels() -> None:
    for name, env_key in (
        ("approvals", "DISCORD_CHANNEL_APPROVALS"),
        ("alerts", "DISCORD_CHANNEL_ALERTS"),
        ("changelog", "DISCORD_CHANNEL_CHANGELOG"),
    ):
        raw = os.getenv(env_key, "")
        if raw:
            try:
                _CHANNEL_IDS[name] = int(raw)
            except ValueError:
                log.warning("invalid channel ID for %s: %r", name, raw)


_resolve_channels()


def channel_id(name: str) -> int:
    cid = _CHANNEL_IDS.get(name)
    if cid is None:
        raise ValueError(f"channel '{name}' not configured — set DISCORD_CHANNEL_{name.upper()}")
    return cid


class BotClient(discord.Client):
    def __init__(self, temporal_client: Client) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # privileged intent required for reply text
        super().__init__(intents=intents)
        self._temporal = temporal_client

    async def on_ready(self) -> None:
        log.info("discord bot ready: %s (id=%s)", self.user, self.user.id)

    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.user:
            return

        # Only process messages inside a thread
        if not isinstance(message.channel, discord.Thread):
            return

        thread_id = str(message.channel.id)
        workflow_id = thread_store.get_workflow(thread_id)
        if not workflow_id:
            return

        log.info(
            "routing reply from %s in thread %s → workflow %s",
            message.author,
            thread_id,
            workflow_id,
        )
        handle = self.get_workflow_handle(workflow_id)
        await handle.signal("human_reply", message.content)

    def get_workflow_handle(self, workflow_id: str):
        return self._temporal.get_workflow_handle(workflow_id)

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
