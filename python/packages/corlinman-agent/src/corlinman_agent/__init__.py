"""corlinman-agent — reasoning loop + context assembly.

Responsibility: drive a chat completion to completion, aggregating
OpenAI-standard JSON tool-call fragments from :class:`ProviderChunk`
streams into :class:`ToolCallEvent` emissions. Custom in-band tool-call
markers are not supported (plan §14 R5) — the one true tool-call
protocol is standard OpenAI JSON.
"""

from __future__ import annotations

from corlinman_agent.reasoning_loop import (
    ChatStart,
    DoneEvent,
    ErrorEvent,
    Event,
    ReasoningLoop,
    TokenEvent,
    ToolCallEvent,
    ToolResult,
)

__all__ = [
    "ChatStart",
    "DoneEvent",
    "ErrorEvent",
    "Event",
    "ReasoningLoop",
    "TokenEvent",
    "ToolCallEvent",
    "ToolResult",
]
