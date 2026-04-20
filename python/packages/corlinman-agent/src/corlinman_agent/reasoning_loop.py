"""Reasoning loop — drives a chat completion with interleaved tool calls.

Consumes a :class:`ChatStart` descriptor, invokes the provider's
``chat_stream``, and emits events that mirror the gRPC ``ServerFrame``
surface:

* :class:`TokenEvent` for each text delta;
* :class:`ToolCallEvent` for every completed OpenAI-standard tool call
  (``tool_call_start`` → ``tool_call_delta``\\* → ``tool_call_end``);
* :class:`DoneEvent` on normal end-of-stream;
* :class:`ErrorEvent` if the provider blows up.

Plan §14 R5 decision: the legacy ``<<<[TOOL_REQUEST]>>>`` regex protocol
is gone. Providers emit :class:`ProviderChunk` values with a fixed
``kind`` vocabulary (``token`` / ``tool_call_start`` /
``tool_call_delta`` / ``tool_call_end`` / ``done``), and this loop
aggregates the tool-call fragments into one event per call.

Tool execution is **not** performed here. The loop yields
:class:`ToolCallEvent` and — optionally — awaits :class:`ToolResult`
values pushed via :meth:`ReasoningLoop.feed_tool_result` before
appending a ``role="tool"`` message and looping back to the provider for
a follow-up turn. Callers that don't feed results (notably the M2
single-shot path) just receive the initial round and a terminal Done /
Error event; real multi-round execution lands with the plugin runtime in
M3.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class ChatStart:
    """Minimal descriptor fed to the reasoning loop."""

    model: str
    messages: Sequence[dict[str, Any]]
    tools: Sequence[dict[str, Any]] = field(default_factory=list)
    session_key: str = ""
    temperature: float | None = None
    max_tokens: int | None = None


@dataclass(slots=True)
class TokenEvent:
    """Token delta emission."""

    text: str
    is_reasoning: bool = False


@dataclass(slots=True)
class ToolCallEvent:
    """Parsed tool-call emission (observed, not executed).

    ``args_json`` is the fully-aggregated JSON argument payload as raw
    bytes (the standard OpenAI ``tool_calls[].function.arguments`` string,
    utf-8 encoded).
    """

    call_id: str
    plugin: str
    tool: str
    args_json: bytes


@dataclass(slots=True)
class DoneEvent:
    """Terminal event; always the last yielded."""

    finish_reason: str = "stop"


@dataclass(slots=True)
class ErrorEvent:
    """Terminal error event."""

    message: str
    reason: str = "unknown"


@dataclass(slots=True)
class ToolResult:
    """Tool-execution result pushed back into the loop by the caller.

    ``content`` is the stringified result payload that becomes the
    ``content`` of the ``role="tool"`` message appended to the chat
    history on the next provider call.
    """

    call_id: str
    content: str
    is_error: bool = False


Event = TokenEvent | ToolCallEvent | DoneEvent | ErrorEvent


# Maximum provider rounds allowed before we short-circuit to avoid runaway
# tool-call loops. Tuned generously; real products usually cap at 3-5.
_MAX_ROUNDS = 8


class ReasoningLoop:
    """Drives one chat turn (or a chain of turns if tool results flow in).

    ``tool_result_timeout`` controls how long :meth:`run` waits for each
    tool result to come back via :meth:`feed_tool_result` before giving up
    and terminating the loop. The default (0.05s) is tuned for the M2
    single-shot path where the servicer does **not** forward tool results
    yet — production wiring in M3 should raise this (5-30s) to accommodate
    real plugin execution.
    """

    def __init__(self, provider: Any, *, tool_result_timeout: float = 0.05) -> None:
        """``provider`` must implement :class:`corlinman_providers.base.CorlinmanProvider`."""
        self._provider = provider
        self._tool_result_timeout = tool_result_timeout
        self._tool_results: asyncio.Queue[ToolResult] = asyncio.Queue()

    def feed_tool_result(self, result: ToolResult) -> None:
        """Push a :class:`ToolResult` for consumption by the next round.

        Non-blocking. Intended to be called from the gateway/servicer when a
        ``ClientFrame.tool_result`` arrives while the loop is still running.
        """
        self._tool_results.put_nowait(result)

    async def run(self, start: ChatStart) -> AsyncIterator[Event]:
        """Execute the loop, yielding events until the stream ends."""
        messages: list[dict[str, Any]] = list(start.messages)
        rounds = 0

        while rounds < _MAX_ROUNDS:
            rounds += 1
            tool_calls_this_round: list[ToolCallEvent] = []
            finish_reason = "stop"

            try:
                async for event in self._run_one_round(start, messages):
                    if isinstance(event, ToolCallEvent):
                        tool_calls_this_round.append(event)
                        yield event
                    elif isinstance(event, DoneEvent):
                        finish_reason = event.finish_reason
                    elif isinstance(event, ErrorEvent):
                        yield event
                        return
                    else:
                        yield event
            except Exception as exc:
                logger.warning("reasoning_loop.error", error=str(exc))
                reason = getattr(exc, "reason", "unknown")
                yield ErrorEvent(message=str(exc), reason=reason)
                return

            # No tool calls → we're done; emit the terminal Done and exit.
            if not tool_calls_this_round:
                yield DoneEvent(finish_reason=finish_reason)
                return

            # Tool calls were emitted. If the caller hasn't wired the
            # feedback channel, we can't make progress; end the loop with
            # the provider's finish_reason (typically "tool_calls") so the
            # gateway sees the terminal frame and the pipeline drains.
            results = await self._collect_results(tool_calls_this_round)
            if results is None:
                yield DoneEvent(finish_reason=finish_reason)
                return

            # Otherwise, append an assistant message recording the calls
            # followed by one tool message per result and keep looping.
            messages = _extend_with_tool_round(
                messages, tool_calls_this_round, results
            )
            if any(_is_awaiting_placeholder(r.content) for r in results):
                # Prevent a doom loop: if every result is a placeholder, the
                # next round will ask for the same tool again.
                yield DoneEvent(finish_reason=finish_reason)
                return

        # Rounds exhausted — surface a terminal Done with "length" so the
        # caller can tell this wasn't a clean end.
        yield DoneEvent(finish_reason="length")

    async def _run_one_round(
        self, start: ChatStart, messages: Sequence[dict[str, Any]]
    ) -> AsyncIterator[Event]:
        """Drive a single provider call, aggregating tool-call fragments."""
        # call_id → (plugin/tool name, args fragments list).
        open_calls: dict[str, list[str]] = {}
        open_names: dict[str, str] = {}
        finish_reason = "stop"

        stream = self._provider.chat_stream(
            model=start.model,
            messages=messages,
            tools=start.tools or None,
            temperature=start.temperature,
            max_tokens=start.max_tokens,
        )
        async for chunk in stream:
            kind = chunk.kind
            if kind == "token" and chunk.text:
                yield TokenEvent(text=chunk.text)
            elif kind == "tool_call_start":
                call_id = chunk.tool_call_id or ""
                if not call_id:
                    continue
                open_calls[call_id] = []
                open_names[call_id] = chunk.tool_name or ""
            elif kind == "tool_call_delta":
                call_id = chunk.tool_call_id or ""
                frag = chunk.arguments_delta or ""
                if call_id in open_calls and frag:
                    open_calls[call_id].append(frag)
            elif kind == "tool_call_end":
                call_id = chunk.tool_call_id or ""
                ev = _finalise_tool_call(call_id, open_calls, open_names)
                if ev is not None:
                    yield ev
            elif kind == "done":
                finish_reason = chunk.finish_reason or "stop"
                # Close any still-open calls the provider forgot to terminate.
                for call_id in list(open_calls.keys()):
                    ev = _finalise_tool_call(call_id, open_calls, open_names)
                    if ev is not None:
                        yield ev
                yield DoneEvent(finish_reason=finish_reason)
                return
        # Provider closed without an explicit `done` chunk — treat as stop.
        for call_id in list(open_calls.keys()):
            ev = _finalise_tool_call(call_id, open_calls, open_names)
            if ev is not None:
                yield ev
        yield DoneEvent(finish_reason="stop")

    async def _collect_results(
        self, calls: list[ToolCallEvent]
    ) -> list[ToolResult] | None:
        """Wait for one :class:`ToolResult` per emitted call.

        Returns ``None`` if no result arrives within
        ``self._tool_result_timeout`` — the caller isn't wired for the
        feedback cycle and the loop should terminate after the current
        round.
        """
        needed = {ev.call_id for ev in calls}
        got: dict[str, ToolResult] = {}
        try:
            while needed - got.keys():
                result = await asyncio.wait_for(
                    self._tool_results.get(), timeout=self._tool_result_timeout
                )
                got[result.call_id] = result
        except TimeoutError:
            return None
        return [got[c.call_id] for c in calls]


def _finalise_tool_call(
    call_id: str,
    open_calls: dict[str, list[str]],
    open_names: dict[str, str],
) -> ToolCallEvent | None:
    """Pop a fully-aggregated call out of ``open_calls`` and yield a
    :class:`ToolCallEvent`. Returns ``None`` if ``call_id`` was unknown."""
    if call_id not in open_calls:
        return None
    frags = open_calls.pop(call_id)
    name = open_names.pop(call_id, "")
    joined = "".join(frags).strip() or "{}"
    # If the provider handed us invalid JSON we still forward the raw bytes
    # unchanged — the executor (future) is allowed to decide what to do.
    try:
        json.loads(joined)
    except json.JSONDecodeError:
        logger.warning(
            "reasoning_loop.bad_tool_args", call_id=call_id, raw=joined[:200]
        )
    return ToolCallEvent(
        call_id=call_id,
        # OpenAI tool_calls don't distinguish plugin vs tool — the name is
        # the tool id, and the plugin-to-tool mapping happens at execute
        # time (M3). For now, plugin == tool == function.name.
        plugin=name,
        tool=name,
        args_json=joined.encode("utf-8"),
    )


def _extend_with_tool_round(
    messages: Sequence[dict[str, Any]],
    calls: list[ToolCallEvent],
    results: list[ToolResult],
) -> list[dict[str, Any]]:
    """Return ``messages`` extended with the assistant tool_calls message
    and one ``role="tool"`` message per result."""
    extended: list[dict[str, Any]] = list(messages)
    extended.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": c.call_id,
                    "type": "function",
                    "function": {
                        "name": c.tool,
                        "arguments": c.args_json.decode("utf-8"),
                    },
                }
                for c in calls
            ],
        }
    )
    for r in results:
        extended.append(
            {
                "role": "tool",
                "tool_call_id": r.call_id,
                "content": r.content,
            }
        )
    return extended


def _is_awaiting_placeholder(content: str) -> bool:
    """Detect the gateway's M2 ``awaiting_plugin_runtime`` placeholder.

    Prevents the loop from burning rounds asking for a tool that the
    runtime cannot yet execute.
    """
    if "awaiting_plugin_runtime" not in content:
        return False
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return True
    return isinstance(payload, dict) and payload.get("status") == "awaiting_plugin_runtime"
