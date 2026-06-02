"""Messaging platform abstraction tests (issue #19).

Verifies the MessagingPlatform protocol and the generic activity wrapper
that any messaging bridge (Discord, Slack, etc.) must conform to.
"""

from __future__ import annotations

from devloop.messaging import (
    ArchiveThreadInput,
    MessagingActivities,
    MessagingPlatform,
    SendMessageInput,
    SendMessageOutput,
    SendNotificationInput,
    StubPlatform,
)


# --------------------------------------------------------------------------- #
# Protocol conformance: StubPlatform satisfies MessagingPlatform
# --------------------------------------------------------------------------- #

def test_stub_platform_is_a_messaging_platform():
    """A minimal stub implementation should satisfy the MessagingPlatform
    protocol so consumers can verify conformance at runtime."""
    stub = StubPlatform()
    assert isinstance(stub, MessagingPlatform)


def test_stub_open_thread_returns_thread_id():
    stub = StubPlatform()
    tid = stub.open_thread("approvals", "test-thread", "hello")
    assert isinstance(tid, str)
    assert len(tid) > 0


def test_stub_post_to_thread_succeeds():
    stub = StubPlatform()
    stub.post_to_thread("thread-123", "notification text")  # no exception


def test_stub_archive_thread_succeeds():
    stub = StubPlatform()
    stub.archive_thread("thread-123")  # no exception


# --------------------------------------------------------------------------- #
# MessagingActivities wraps any MessagingPlatform
# --------------------------------------------------------------------------- #

def test_messaging_activities_exposes_send_message():
    """The activity wrapper should expose the three core Temporal activities."""
    platform = StubPlatform()
    acts = MessagingActivities(platform)
    assert hasattr(acts, "send_message")
    assert hasattr(acts, "send_notification")
    assert hasattr(acts, "archive_thread")


def test_send_message_activity_calls_open_thread_and_returns_output():
    """send_message activity delegates to platform.open_thread and wraps the
    result in SendMessageOutput."""
    stub = StubPlatform()
    acts = MessagingActivities(stub)

    inp = SendMessageInput(
        workflow_id="wf-001",
        message="plan ready",
        channel="approvals",
        thread_name="test-plan",
    )
    result = acts.send_message_sync(inp)
    assert isinstance(result, SendMessageOutput)
    assert isinstance(result.thread_id, str)
    assert len(result.thread_id) > 0


def test_send_notification_activity_calls_post_to_thread():
    stub = StubPlatform()
    acts = MessagingActivities(stub)

    inp = SendNotificationInput(workflow_id="wf-001", message="review done")
    acts.send_notification_sync(inp)  # no exception


def test_archive_thread_activity_calls_archive():
    stub = StubPlatform()
    acts = MessagingActivities(stub)

    inp = ArchiveThreadInput(workflow_id="wf-001")
    acts.archive_thread_sync(inp)  # no exception


# --------------------------------------------------------------------------- #
# Activity signatures round-trip correctly
# --------------------------------------------------------------------------- #

def test_send_message_input_roundtrip():
    """Activity input dataclasses carry the expected fields."""
    inp = SendMessageInput(
        workflow_id="wf-test",
        message="approve?",
        channel="alerts",
        thread_name="my-plan",
    )
    assert inp.workflow_id == "wf-test"
    assert inp.message == "approve?"
    assert inp.channel == "alerts"
    assert inp.thread_name == "my-plan"


def test_send_message_output_roundtrip():
    out = SendMessageOutput(thread_id="slack-abc123")
    assert out.thread_id == "slack-abc123"


def test_send_notification_input_roundtrip():
    inp = SendNotificationInput(workflow_id="wf-notify", message="deployed")
    assert inp.workflow_id == "wf-notify"
    assert inp.message == "deployed"


def test_archive_thread_input_roundtrip():
    inp = ArchiveThreadInput(workflow_id="wf-archive")
    assert inp.workflow_id == "wf-archive"


# --------------------------------------------------------------------------- #
# StubPlatform records calls for verification
# --------------------------------------------------------------------------- #

def test_stub_records_open_thread_calls():
    stub = StubPlatform()
    stub.open_thread("channel-1", "name", "msg")
    assert len(stub.calls["open_thread"]) == 1
    call = stub.calls["open_thread"][0]
    assert call["channel_name"] == "channel-1"
    assert call["thread_name"] == "name"
    assert call["initial_message"] == "msg"


def test_stub_records_post_to_thread_calls():
    stub = StubPlatform()
    stub.post_to_thread("t-1", "hello")
    assert len(stub.calls["post_to_thread"]) == 1
    assert stub.calls["post_to_thread"][0]["thread_id"] == "t-1"
    assert stub.calls["post_to_thread"][0]["message"] == "hello"


def test_stub_records_archive_thread_calls():
    stub = StubPlatform()
    stub.archive_thread("t-99")
    assert len(stub.calls["archive_thread"]) == 1
    assert stub.calls["archive_thread"][0]["thread_id"] == "t-99"
