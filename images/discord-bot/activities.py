"""Temporal Activities for the Discord Bot worker.

Activities run in the discord-bot task queue. The orchestration worker calls
these to post messages/notifications and archive threads.

Signal contract (sent by the Discord gateway, not these activities):
  signal name : "human_reply"
  payload     : str  (the raw reply text from Discord)
"""

import dataclasses
import logging

from temporalio import activity

import thread_store
from discord_client import BotClient

log = logging.getLogger(__name__)


@dataclasses.dataclass
class SendMessageInput:
    workflow_id: str
    message: str
    channel: str = "approvals"
    thread_name: str = ""


@dataclasses.dataclass
class SendMessageOutput:
    thread_id: str


@dataclasses.dataclass
class SendNotificationInput:
    workflow_id: str
    message: str


@dataclasses.dataclass
class ArchiveThreadInput:
    workflow_id: str


class DiscordActivities:
    """Temporal Activity implementations that need access to the Discord client."""

    def __init__(self, bot: BotClient) -> None:
        self._bot = bot

    @activity.defn(name="send_message")
    async def send_message(self, inp: SendMessageInput) -> SendMessageOutput:
        """Open (or reuse) a Discord thread, post a message, and store the mapping.

        The calling workflow should park itself on a `human_reply` signal after
        this returns. The Discord gateway forwards the first thread reply to
        the workflow via that signal.
        """
        existing_thread_id = thread_store.get_thread(inp.workflow_id)
        if existing_thread_id:
            await self._bot.post_to_thread(existing_thread_id, inp.message)
            activity.logger.info("posted to existing thread %s", existing_thread_id)
            return SendMessageOutput(thread_id=existing_thread_id)

        thread_name = inp.thread_name or f"workflow {inp.workflow_id[:8]}"
        thread = await self._bot.open_thread(inp.channel, thread_name, inp.message)
        thread_store.put(inp.workflow_id, str(thread.id))
        activity.logger.info("opened thread %s for workflow %s", thread.id, inp.workflow_id)
        return SendMessageOutput(thread_id=str(thread.id))

    @activity.defn(name="send_notification")
    async def send_notification(self, inp: SendNotificationInput) -> None:
        """Post a message to the workflow's thread without expecting a reply."""
        thread_id = thread_store.get_thread(inp.workflow_id)
        if not thread_id:
            activity.logger.warning(
                "no thread found for workflow %s — notification dropped", inp.workflow_id
            )
            return
        await self._bot.post_to_thread(thread_id, inp.message)

    @activity.defn(name="archive_thread")
    async def archive_thread(self, inp: ArchiveThreadInput) -> None:
        """Lock and archive the Discord thread for a completed workflow."""
        thread_id = thread_store.get_thread(inp.workflow_id)
        if not thread_id:
            activity.logger.warning("no thread found for workflow %s — nothing to archive", inp.workflow_id)
            return
        await self._bot.archive_thread(thread_id)
        thread_store.delete(inp.workflow_id)
