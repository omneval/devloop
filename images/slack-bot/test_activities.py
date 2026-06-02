"""Tests for SlackActivities conformance to MessagingPlatform and
Temporal activity wrapper integration.

Verifies that SlackActivities wraps a platform implementing MessagingPlatform
and delegates correctly through MessagingActivities.

Run with:
    cd images/slack-bot
    uv run --with pytest --with pytest-asyncio pytest -q test_activities.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure src/devloop is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from devloop.messaging import (
    ArchiveThreadInput,
    MessagingActivities,
    MessagingPlatform,
    SendMessageInput,
    SendMessageOutput,
    SendNotificationInput,
    StubPlatform,
)


class MockSlackClient:
    """Minimal MockSlackClient that satisfies MessagingPlatform for tests."""

    def __init__(self) -> None:
        self.threads: dict[str, str] = {}
        self.posts: list[tuple[str, str]] = []
        self.archives: list[str] = []

    def open_thread(
        self, channel_name: str, thread_name: str, initial_message: str
    ) -> str:
        tid = f"C0123:1677536646.000{len(self.threads)}"
        self.threads[tid] = channel_name
        return tid

    def post_to_thread(self, thread_id: str, message: str) -> None:
        self.posts.append((thread_id, message))

    def archive_thread(self, thread_id: str) -> None:
        self.archives.append(thread_id)


def test_mock_slack_client_is_messaging_platform():
    """MockSlackClient should satisfy MessagingPlatform protocol."""
    client = MockSlackClient()
    assert isinstance(client, MessagingPlatform)


def test_slack_activities_send_message():
    """SlackActivities.send_message delegates through MessagingActivities."""
    from activities import SlackActivities

    client = MockSlackClient()
    acts = SlackActivities(client)

    inp = SendMessageInput(
        workflow_id="wf-slack-001",
        message="Plan ready for review",
        channel="approvals",
        thread_name="wf-slack-001-plan",
    )
    result = acts.send_message(inp)
    assert isinstance(result, SendMessageOutput)
    assert result.thread_id.startswith("C0123:")
    assert len(client.threads) == 1


def test_slack_activities_send_notification():
    """SlackActivities.send_notification posts to existing thread."""
    from activities import SlackActivities

    client = MockSlackClient()
    acts = SlackActivities(client)

    # First open a thread
    acts.send_message(
        SendMessageInput(
            workflow_id="wf-notify",
            message="initial",
            channel="alerts",
            thread_name="wf-notify",
        )
    )
    # Then send a notification
    acts.send_notification(
        SendNotificationInput(workflow_id="wf-notify", message="review done")
    )
    assert len(client.posts) == 1
    assert client.posts[0][1] == "review done"


def test_slack_activities_archive_thread():
    """SlackActivities.archive_thread archives the workflow thread."""
    from activities import SlackActivities

    client = MockSlackClient()
    acts = SlackActivities(client)

    acts.send_message(
        SendMessageInput(
            workflow_id="wf-archive",
            message="start",
            channel="approvals",
            thread_name="wf-archive",
        )
    )
    acts.archive_thread(ArchiveThreadInput(workflow_id="wf-archive"))
    assert len(client.archives) == 1


def test_slack_activities_has_activity_methods():
    """SlackActivities exposes the three core activity methods."""
    from activities import SlackActivities

    client = MockSlackClient()
    acts = SlackActivities(client)
    assert hasattr(acts, "send_message")
    assert hasattr(acts, "send_notification")
    assert hasattr(acts, "archive_thread")


def test_messaging_activities_with_mock_slack():
    """MessagingActivities works with any MessagingPlatform, including Slack."""
    client = MockSlackClient()
    wrapper = MessagingActivities(platform=client)

    # send_message
    out = wrapper.send_message(
        SendMessageInput(
            workflow_id="wf-test",
            message="hello slack",
            channel="approvals",
            thread_name="wf-test",
        )
    )
    assert isinstance(out, SendMessageOutput)

    # send_notification reuses the same thread
    wrapper.send_notification(
        SendNotificationInput(workflow_id="wf-test", message="update")
    )
    assert len(client.posts) == 1

    # archive
    wrapper.archive_thread(ArchiveThreadInput(workflow_id="wf-test"))
    assert len(client.archives) == 1
