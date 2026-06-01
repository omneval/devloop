"""Pure text helpers for the Discord bot (no discord.py import, so unit-testable
without the gateway library).

Discord's API rejects messages over 2000 chars and thread names over 100 chars
with 400 "Invalid Form Body" (error 50035), which previously failed whole
workflows — e.g. an Alert Response diagnosis whose root-cause text ran past the
message limit. The client clamps all outgoing content through ``clamp``.
"""

from __future__ import annotations

MAX_MESSAGE = 2000
MAX_THREAD_NAME = 100
TRUNC_MARKER = "\n… [truncated]"


def clamp(text: str, limit: int, marker: str = "") -> str:
    """Truncate ``text`` to ``limit`` chars, appending ``marker`` when it fits."""
    text = text or ""
    if len(text) <= limit:
        return text
    if marker and limit > len(marker):
        return text[: limit - len(marker)] + marker
    return text[:limit]
