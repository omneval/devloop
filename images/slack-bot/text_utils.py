"""Pure text helpers for the Slack bot (no slack-bolt import, so unit-testable
without the gateway library).

Slack's API rejects messages over 3000 chars and channel/thread topic names
over 80 chars with ``invalid_post_message`` / ``channel_name_invalid``.
The client clamps all outgoing content through ``clamp``.
"""

from __future__ import annotations

MAX_MESSAGE = 3000
MAX_THREAD_TOPIC = 80
TRUNC_MARKER = "\n… [truncated]"


def clamp(text: str, limit: int, marker: str = "") -> str:
    """Truncate ``text`` to ``limit`` chars, appending ``marker`` when it fits."""
    text = text or ""
    if len(text) <= limit:
        return text
    if marker and limit > len(marker):
        return text[: limit - len(marker)] + marker
    return text[:limit]
