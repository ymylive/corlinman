"""corlinman-agent — reasoning loop + context assembly.

Responsibility: drive a chat completion to completion, aggregating
OpenAI-standard JSON tool-call fragments from :class:`ProviderChunk`
streams into :class:`ToolCallEvent` emissions. Custom in-band tool-call
markers are not supported (plan §14 R5) — the one true tool-call
protocol is standard OpenAI JSON.

Sprint 9 T3 additions: :class:`SessionQueryClient` is a read-only
client for the gateway's SQLite session store, used by future S12
sub-agent orchestration and S16 DeepMemo layers to fetch past turns.
"""

from __future__ import annotations

from corlinman_agent.reasoning_loop import (
    Attachment,
    ChatStart,
    DoneEvent,
    ErrorEvent,
    Event,
    ReasoningLoop,
    TokenEvent,
    ToolCallEvent,
    ToolResult,
)
from corlinman_agent.session_query import (
    SessionMessage,
    SessionQueryClient,
    SessionQueryError,
    SessionRole,
)

__all__ = [
    "Attachment",
    "ChatStart",
    "DoneEvent",
    "ErrorEvent",
    "Event",
    "ReasoningLoop",
    "SessionMessage",
    "SessionQueryClient",
    "SessionQueryError",
    "SessionRole",
    "TokenEvent",
    "ToolCallEvent",
    "ToolResult",
]
