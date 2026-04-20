"""Reasoning loop unit tests — aggregate ProviderChunk streams into events.

The loop consumes a provider object that matches the :class:`CorlinmanProvider`
Protocol; we substitute a minimal async-iterator stub that yields
:class:`ProviderChunk` values so these tests stay offline.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from corlinman_agent import (
    Attachment,
    ChatStart,
    DoneEvent,
    ErrorEvent,
    ReasoningLoop,
    TokenEvent,
    ToolCallEvent,
    ToolResult,
)
from corlinman_providers.base import ProviderChunk


class _FakeProvider:
    """Emits a preset list of ProviderChunk values."""

    def __init__(self, chunks: list[ProviderChunk]) -> None:
        self._chunks = chunks

    async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        for c in self._chunks:
            yield c


class _ExplodingProvider:
    async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        yield ProviderChunk(kind="token", text="partial")
        raise RuntimeError("provider blew up")


class _MultiRoundProvider:
    """Yields a different chunk list per call — used to test tool-result feedback."""

    def __init__(self, rounds: list[list[ProviderChunk]]) -> None:
        self._rounds = rounds
        self.calls_seen: list[list[dict[str, Any]]] = []

    async def chat_stream(
        self, *, messages: list[dict[str, Any]], **_: Any
    ) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        self.calls_seen.append(list(messages))
        idx = len(self.calls_seen) - 1
        if idx >= len(self._rounds):
            yield ProviderChunk(kind="done", finish_reason="stop")
            return
        for c in self._rounds[idx]:
            yield c


async def _collect(loop: ReasoningLoop, start: ChatStart) -> list:
    events = []
    async for e in loop.run(start):
        events.append(e)
    return events


@pytest.mark.asyncio
async def test_pure_text_stream() -> None:
    prov = _FakeProvider(
        [
            ProviderChunk(kind="token", text="hello "),
            ProviderChunk(kind="token", text="world"),
            ProviderChunk(kind="done", finish_reason="stop"),
        ]
    )
    events = await _collect(ReasoningLoop(prov), ChatStart(model="x", messages=[]))
    tokens = [e.text for e in events if isinstance(e, TokenEvent)]
    assert tokens == ["hello ", "world"]
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_single_tool_call_aggregated() -> None:
    prov = _FakeProvider(
        [
            ProviderChunk(kind="token", text="ok, calling "),
            ProviderChunk(
                kind="tool_call_start",
                tool_call_id="call_abc",
                tool_name="FooPlugin",
            ),
            ProviderChunk(
                kind="tool_call_delta",
                tool_call_id="call_abc",
                arguments_delta='{"query":',
            ),
            ProviderChunk(
                kind="tool_call_delta",
                tool_call_id="call_abc",
                arguments_delta='"hi"}',
            ),
            ProviderChunk(kind="tool_call_end", tool_call_id="call_abc"),
            ProviderChunk(kind="done", finish_reason="tool_calls"),
        ]
    )
    events = await _collect(ReasoningLoop(prov), ChatStart(model="x", messages=[]))
    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].call_id == "call_abc"
    assert tool_events[0].plugin == "FooPlugin"
    args = json.loads(tool_events[0].args_json.decode("utf-8"))
    assert args == {"query": "hi"}


@pytest.mark.asyncio
async def test_multiple_tool_calls_aggregated() -> None:
    prov = _FakeProvider(
        [
            ProviderChunk(kind="tool_call_start", tool_call_id="a", tool_name="A"),
            ProviderChunk(kind="tool_call_delta", tool_call_id="a", arguments_delta="{}"),
            ProviderChunk(kind="tool_call_end", tool_call_id="a"),
            ProviderChunk(kind="tool_call_start", tool_call_id="b", tool_name="B"),
            ProviderChunk(kind="tool_call_delta", tool_call_id="b", arguments_delta="{}"),
            ProviderChunk(kind="tool_call_end", tool_call_id="b"),
            ProviderChunk(kind="done", finish_reason="tool_calls"),
        ]
    )
    events = await _collect(ReasoningLoop(prov), ChatStart(model="x", messages=[]))
    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert [e.plugin for e in tool_events] == ["A", "B"]
    assert [e.call_id for e in tool_events] == ["a", "b"]


@pytest.mark.asyncio
async def test_missing_tool_call_end_still_flushes_at_done() -> None:
    """Provider forgets to emit ``tool_call_end`` — the loop still finalises
    the open call when ``done`` arrives."""
    prov = _FakeProvider(
        [
            ProviderChunk(kind="tool_call_start", tool_call_id="x", tool_name="X"),
            ProviderChunk(kind="tool_call_delta", tool_call_id="x", arguments_delta='{"k":1}'),
            ProviderChunk(kind="done", finish_reason="tool_calls"),
        ]
    )
    events = await _collect(ReasoningLoop(prov), ChatStart(model="x", messages=[]))
    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].call_id == "x"


@pytest.mark.asyncio
async def test_provider_exception_emits_error_event() -> None:
    events = await _collect(ReasoningLoop(_ExplodingProvider()), ChatStart(model="x", messages=[]))
    assert any(isinstance(e, ErrorEvent) for e in events)
    assert not any(isinstance(e, DoneEvent) for e in events)


@pytest.mark.asyncio
async def test_token_then_tool_call_then_token_across_round() -> None:
    """Tokens and tool_calls interleave correctly in a single round."""
    prov = _FakeProvider(
        [
            ProviderChunk(kind="token", text="prefix "),
            ProviderChunk(kind="tool_call_start", tool_call_id="t1", tool_name="Tool"),
            ProviderChunk(kind="tool_call_delta", tool_call_id="t1", arguments_delta="{}"),
            ProviderChunk(kind="tool_call_end", tool_call_id="t1"),
            ProviderChunk(kind="token", text=" suffix"),
            ProviderChunk(kind="done", finish_reason="tool_calls"),
        ]
    )
    events = await _collect(ReasoningLoop(prov), ChatStart(model="x", messages=[]))
    kinds = [type(e).__name__ for e in events]
    # TokenEvent -> ToolCallEvent -> TokenEvent -> DoneEvent
    assert kinds == ["TokenEvent", "ToolCallEvent", "TokenEvent", "DoneEvent"]


@pytest.mark.asyncio
async def test_no_tool_call_ends_with_stop() -> None:
    prov = _FakeProvider([ProviderChunk(kind="done", finish_reason="stop")])
    events = await _collect(ReasoningLoop(prov), ChatStart(model="x", messages=[]))
    assert len(events) == 1
    assert isinstance(events[0], DoneEvent)
    assert events[0].finish_reason == "stop"


@pytest.mark.asyncio
async def test_tool_result_drives_second_round() -> None:
    """After yielding a ToolCallEvent, feeding a ToolResult triggers another
    provider call with the tool message appended."""
    round1 = [
        ProviderChunk(kind="tool_call_start", tool_call_id="c1", tool_name="t"),
        ProviderChunk(kind="tool_call_delta", tool_call_id="c1", arguments_delta="{}"),
        ProviderChunk(kind="tool_call_end", tool_call_id="c1"),
        ProviderChunk(kind="done", finish_reason="tool_calls"),
    ]
    round2 = [
        ProviderChunk(kind="token", text="done"),
        ProviderChunk(kind="done", finish_reason="stop"),
    ]
    prov = _MultiRoundProvider([round1, round2])
    loop = ReasoningLoop(prov)

    events: list = []

    async def driver() -> None:
        async for e in loop.run(ChatStart(model="x", messages=[{"role": "user", "content": "hi"}])):
            events.append(e)
            if isinstance(e, ToolCallEvent):
                loop.feed_tool_result(ToolResult(call_id=e.call_id, content='{"ok":true}'))

    await asyncio.wait_for(driver(), timeout=2.0)

    # Two rounds happened: the second call saw the tool result appended.
    assert len(prov.calls_seen) == 2
    round2_messages = prov.calls_seen[1]
    assert round2_messages[-1]["role"] == "tool"
    assert round2_messages[-1]["tool_call_id"] == "c1"
    # And the overall event stream ends with a DoneEvent(finish_reason="stop").
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_awaiting_placeholder_result_ends_loop() -> None:
    """If the gateway echoes ``awaiting_plugin_runtime`` the loop must stop
    after the first round — otherwise the model would re-request the tool."""
    round1 = [
        ProviderChunk(kind="tool_call_start", tool_call_id="c1", tool_name="t"),
        ProviderChunk(kind="tool_call_end", tool_call_id="c1"),
        ProviderChunk(kind="done", finish_reason="tool_calls"),
    ]
    prov = _MultiRoundProvider([round1])
    loop = ReasoningLoop(prov)

    events: list = []

    async def driver() -> None:
        async for e in loop.run(ChatStart(model="x", messages=[])):
            events.append(e)
            if isinstance(e, ToolCallEvent):
                loop.feed_tool_result(
                    ToolResult(
                        call_id=e.call_id,
                        content='{"status":"awaiting_plugin_runtime"}',
                    )
                )

    await asyncio.wait_for(driver(), timeout=2.0)
    # Exactly one provider round; loop terminated without a follow-up call.
    assert len(prov.calls_seen) == 1
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_attachments_forwarded_as_content_parts() -> None:
    """ChatStart.attachments rewrite the trailing user turn's content into
    OpenAI-shape multi-part blocks before the provider sees it."""
    prov = _MultiRoundProvider(
        [[ProviderChunk(kind="done", finish_reason="stop")]]
    )
    loop = ReasoningLoop(prov)
    start = ChatStart(
        model="x",
        messages=[{"role": "user", "content": "look at this"}],
        attachments=[
            Attachment(kind="image", url="https://cdn/pic.png", mime="image/png"),
        ],
    )
    await _collect(loop, start)
    # Exactly one round; the provider saw the rewritten user message.
    assert len(prov.calls_seen) == 1
    msgs = prov.calls_seen[0]
    assert len(msgs) == 1
    content = msgs[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "look at this"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "https://cdn/pic.png"},
    }


@pytest.mark.asyncio
async def test_attachments_none_leaves_messages_unchanged() -> None:
    """Without attachments the loop must not touch the original messages."""
    prov = _MultiRoundProvider(
        [[ProviderChunk(kind="done", finish_reason="stop")]]
    )
    loop = ReasoningLoop(prov)
    msg = {"role": "user", "content": "plain text"}
    await _collect(loop, ChatStart(model="x", messages=[msg]))
    assert prov.calls_seen[0][0]["content"] == "plain text"


@pytest.mark.asyncio
async def test_attachments_audio_forwarded_as_file_part() -> None:
    """Non-image attachments land as a generic ``file`` content part so the
    provider adapter (not the loop) decides whether to skip or translate."""
    prov = _MultiRoundProvider(
        [[ProviderChunk(kind="done", finish_reason="stop")]]
    )
    loop = ReasoningLoop(prov)
    start = ChatStart(
        model="x",
        messages=[{"role": "user", "content": "voice note"}],
        attachments=[Attachment(kind="audio", url="https://cdn/v.amr")],
    )
    await _collect(loop, start)
    content = prov.calls_seen[0][0]["content"]
    assert isinstance(content, list)
    assert any(p.get("type") == "file" for p in content)
    file_part = next(p for p in content if p.get("type") == "file")
    assert file_part["file"]["kind"] == "audio"
    assert file_part["file"]["url"] == "https://cdn/v.amr"


@pytest.mark.asyncio
async def test_attachment_image_bytes_become_data_url() -> None:
    """Attachment with bytes (no url) encodes into a data: URI."""
    prov = _MultiRoundProvider(
        [[ProviderChunk(kind="done", finish_reason="stop")]]
    )
    loop = ReasoningLoop(prov)
    raw = b"\x89PNGFAKE"
    start = ChatStart(
        model="x",
        messages=[{"role": "user", "content": ""}],
        attachments=[Attachment(kind="image", bytes_=raw, mime="image/png")],
    )
    await _collect(loop, start)
    content = prov.calls_seen[0][0]["content"]
    assert isinstance(content, list)
    img = next(p for p in content if p.get("type") == "image_url")
    url = img["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
