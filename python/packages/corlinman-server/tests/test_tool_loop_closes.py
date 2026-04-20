"""Integration test: Chat servicer closes the tool-call → tool-result loop.

Exercises ``CorlinmanAgentServicer.Chat`` end-to-end over a real in-process
gRPC channel:

* Round 1 — provider emits ``tool_call_start`` / ``tool_call_end`` and
  ``done(tool_calls)``; the servicer yields a ``ServerFrame.tool_call``.
* Client replies with a ``ClientFrame.tool_result`` carrying the fake
  plugin's payload.
* Round 2 — the provider's next ``chat_stream`` call (gated by the fake)
  sees the ``role="tool"`` message appended and streams a token + final
  ``done(stop)``. The servicer surfaces the token and terminal Done frame.

Also covers client-side cancellation: a ``ClientFrame.cancel`` sent while
the loop is mid-round drives ``ReasoningLoop.cancel`` → the servicer emits
an ``ErrorInfo(reason=CANCELLED)`` and closes cleanly.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import grpc
import grpc.aio
import pytest
from corlinman_grpc import agent_pb2, agent_pb2_grpc, common_pb2
from corlinman_providers.base import ProviderChunk
from corlinman_server.agent_servicer import CorlinmanAgentServicer


class _GatedMultiRoundProvider:
    """Provider that yields a different chunk list per ``chat_stream`` call.

    Round 2 blocks on an ``asyncio.Event`` so the test can assert the
    servicer genuinely waits for the inbound ``ToolResult`` before making
    the follow-up provider call.
    """

    def __init__(self, rounds: list[list[ProviderChunk]]) -> None:
        self._rounds = rounds
        self._call_count = 0
        self.round2_gate = asyncio.Event()
        self.messages_per_round: list[list[dict[str, Any]]] = []

    async def chat_stream(
        self, *, messages: list[dict[str, Any]], **_: Any
    ) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        idx = self._call_count
        self._call_count += 1
        self.messages_per_round.append(list(messages))
        if idx == 1:
            # Wait for the client to have fed a ToolResult before streaming.
            await self.round2_gate.wait()
        if idx >= len(self._rounds):
            yield ProviderChunk(kind="done", finish_reason="stop")
            return
        for c in self._rounds[idx]:
            yield c


class _ToolCallThenWaitProvider:
    """Emits a single tool_call then done(tool_calls) — loop blocks on result.

    Used to test cancel: after the loop yields ``ToolCall`` it waits in
    :meth:`ReasoningLoop._collect_results` for a ``ToolResult``; a cancel
    frame arriving at that point unblocks the wait and yields an
    :class:`ErrorEvent` with ``reason="cancelled"``.
    """

    async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        yield ProviderChunk(
            kind="tool_call_start", tool_call_id="call_x", tool_name="t"
        )
        yield ProviderChunk(
            kind="tool_call_delta", tool_call_id="call_x", arguments_delta="{}"
        )
        yield ProviderChunk(kind="tool_call_end", tool_call_id="call_x")
        yield ProviderChunk(kind="done", finish_reason="tool_calls")


@pytest.mark.asyncio
async def test_tool_call_result_round_trip_advances_loop() -> None:
    """ChatStart → ToolCall → ToolResult → Token + Done."""
    round1 = [
        ProviderChunk(
            kind="tool_call_start",
            tool_call_id="call_1",
            tool_name="echo.greet",
        ),
        ProviderChunk(
            kind="tool_call_delta",
            tool_call_id="call_1",
            arguments_delta='{"name":"Ada"}',
        ),
        ProviderChunk(kind="tool_call_end", tool_call_id="call_1"),
        ProviderChunk(kind="done", finish_reason="tool_calls"),
    ]
    round2 = [
        ProviderChunk(kind="token", text="hi Ada"),
        ProviderChunk(kind="done", finish_reason="stop"),
    ]
    provider = _GatedMultiRoundProvider([round1, round2])

    def _resolver(_model: str) -> Any:
        return provider

    servicer = CorlinmanAgentServicer(provider_resolver=_resolver)
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = agent_pb2_grpc.AgentStub(channel)

            # Queue used by the client-side generator to feed post-start frames.
            outbound: asyncio.Queue[agent_pb2.ClientFrame | None] = asyncio.Queue()
            await outbound.put(
                agent_pb2.ClientFrame(
                    start=agent_pb2.ChatStart(
                        model="claude-sonnet-4-5",
                        messages=[
                            common_pb2.Message(role=common_pb2.USER, content="hi")
                        ],
                    )
                )
            )

            async def frames() -> AsyncIterator[agent_pb2.ClientFrame]:
                while True:
                    item = await outbound.get()
                    if item is None:
                        return
                    yield item

            call = stub.Chat(frames())
            received_tokens: list[str] = []
            kinds: list[str] = []
            async for f in call:
                kind = f.WhichOneof("kind")
                kinds.append(kind)
                if kind == "tool_call":
                    # Client reacts: send ToolResult and release round 2.
                    await outbound.put(
                        agent_pb2.ClientFrame(
                            tool_result=agent_pb2.ToolResult(
                                call_id=f.tool_call.call_id,
                                result_json=b'{"greeting":"hi Ada"}',
                                is_error=False,
                                duration_ms=5,
                            )
                        )
                    )
                    provider.round2_gate.set()
                elif kind == "token":
                    received_tokens.append(f.token.text)
                elif kind == "done":
                    # Close client half so the server can shut down cleanly.
                    await outbound.put(None)

            assert received_tokens == ["hi Ada"]
            assert kinds[-1] == "done"
            assert "tool_call" in kinds
            # Servicer made two provider calls; round 2 saw the tool message.
            assert len(provider.messages_per_round) == 2
            round2_msgs = provider.messages_per_round[1]
            assert round2_msgs[-1]["role"] == "tool"
            assert round2_msgs[-1]["tool_call_id"] == "call_1"
    finally:
        await server.stop(grace=None)


@pytest.mark.asyncio
async def test_cancel_frame_terminates_stream_with_error() -> None:
    """Client sends ``cancel`` while provider is mid-stream → ErrorInfo."""
    def _resolver(_model: str) -> Any:
        return _ToolCallThenWaitProvider()

    servicer = CorlinmanAgentServicer(provider_resolver=_resolver)
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = agent_pb2_grpc.AgentStub(channel)

            outbound: asyncio.Queue[agent_pb2.ClientFrame | None] = asyncio.Queue()
            await outbound.put(
                agent_pb2.ClientFrame(
                    start=agent_pb2.ChatStart(model="claude-sonnet-4-5")
                )
            )

            async def frames() -> AsyncIterator[agent_pb2.ClientFrame]:
                while True:
                    item = await outbound.get()
                    if item is None:
                        return
                    yield item

            call = stub.Chat(frames())
            kinds: list[str] = []
            error_reason: int | None = None

            async def drive() -> None:
                nonlocal error_reason
                async for f in call:
                    kind = f.WhichOneof("kind")
                    kinds.append(kind)
                    if kind == "tool_call":
                        # Loop is now waiting for a ToolResult; cancel instead.
                        await outbound.put(
                            agent_pb2.ClientFrame(
                                cancel=agent_pb2.Cancel(reason="user_abort")
                            )
                        )
                    elif kind == "error":
                        error_reason = f.error.reason
                        await outbound.put(None)
                        return
                    elif kind == "done":
                        await outbound.put(None)
                        return

            await asyncio.wait_for(drive(), timeout=5.0)

            # Terminal frame must be an error (from the cancel), not a Done.
            assert kinds[-1] == "error"
            assert "done" not in kinds
            assert "tool_call" in kinds
            assert error_reason is not None
    finally:
        await server.stop(grace=None)
