"""Temporal Activities for the Slack Bot worker.

Activities run on the slack-bot task queue. The orchestration worker calls
these to post messages/notifications and close threads.

Uses the generic ``MessagingActivities`` wrapper from ``devloop.messaging``,
wired to a ``SlackPlatform`` that implements the ``MessagingPlatform`` protocol.

Signal contract (sent by the Slack gateway, not these activities):
  signal name : "human_reply"
  payload     : str  (the raw reply text from Slack)
"""

import logging

from temporalio import activity

from devloop.messaging import (
    ArchiveThreadInput,
    MessagingActivities,
    SendMessageInput,
    SendMessageOutput,
    SendNotificationInput,
)

import thread_store
from slack_client import BotClient

log = logging.getLogger(__name__)


class SlackActivities:
    """Temporal Activity implementations that use the generic messaging wrapper."""

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
