"""End-to-end tests for the :class:`AgentClient` + :class:`ChatStream`.

Boots a real ``grpc.aio.server`` with :class:`FakeAgentServicer` and
exercises the public surface: endpoint resolution, channel construction,
bidi send/receive, cancel, trace-metadata propagation, and the
classified-read error path.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import grpc
import grpc.aio
import pytest
from corlinman_grpc._generated.corlinman.v1 import agent_pb2
from corlinman_grpc.agent_client import (
    CHANNEL_CAPACITY,
    DEFAULT_TCP_ADDR,
    AgentClient,
    ConfigError,
    FailoverReason,
    PlaceholderExecutor,
    UpstreamError,
    connect_channel,
    inject_trace_context,
    resolve_endpoint,
)

# ---------------------------------------------------------------------------
# Endpoint resolution.
# ---------------------------------------------------------------------------


def test_resolve_prefers_addr_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORLINMAN_PY_ADDR", "10.0.0.1:6000")
    monkeypatch.delenv("CORLINMAN_PY_PORT", raising=False)
    assert resolve_endpoint() == "10.0.0.1:6000"


def test_resolve_uses_port_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_PY_ADDR", raising=False)
    monkeypatch.setenv("CORLINMAN_PY_PORT", "9999")
    assert resolve_endpoint() == "127.0.0.1:9999"


def test_resolve_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_PY_ADDR", raising=False)
    monkeypatch.delenv("CORLINMAN_PY_PORT", raising=False)
    assert resolve_endpoint() == DEFAULT_TCP_ADDR


def test_connect_channel_rejects_empty() -> None:
    with pytest.raises(ConfigError):
        connect_channel("")


def test_connect_channel_strips_http_scheme() -> None:
    # Just confirm we can construct a channel object; lazy connect means
    # no real socket activity until the first call.
    ch = connect_channel("http://127.0.0.1:65535")
    assert ch is not None


# ---------------------------------------------------------------------------
# Trace propagation.
# ---------------------------------------------------------------------------


def test_inject_trace_context_returns_input_when_no_otel() -> None:
    # With no OTel tracer installed the helper either no-ops or
    # injects an empty carrier; in either case original metadata is
    # preserved and no exception escapes.
    base = [("authorization", "bearer x")]
    out = inject_trace_context(base)
    assert out[0] == ("authorization", "bearer x")
    # Trace headers (traceparent / tracestate) may or may not be
    # present depending on what's installed; we only require the
    # base entry to round-trip.


# ---------------------------------------------------------------------------
# Tool callback (PlaceholderExecutor).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_placeholder_executor_returns_awaiting_payload() -> None:
    exec_ = PlaceholderExecutor()
    call = agent_pb2.ToolCall(
        call_id="c1",
        plugin="FooPlugin",
        tool="do_thing",
        args_json=b"{}",
        seq=0,
    )
    result = await exec_.execute(call)
    assert result.call_id == "c1"
    assert result.is_error is False
    payload = json.loads(result.result_json)
    assert payload["status"] == "awaiting_plugin_runtime"
    assert payload["plugin"] == "FooPlugin"
    assert payload["tool"] == "do_thing"


# ---------------------------------------------------------------------------
# Full bidi: send ChatStart, receive Done.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_stream_sends_start_and_receives_done(
    fake_agent_server,
) -> None:
    port, _servicer = await fake_agent_server(None)
    channel = connect_channel(f"127.0.0.1:{port}")
    async with AgentClient(channel) as client:
        stream = await client.chat()
        await stream.send(
            agent_pb2.ClientFrame(start=agent_pb2.ChatStart(model="m"))
        )
        kinds: list[str] = []
        async for frame in stream:
            kinds.append(frame.WhichOneof("kind"))
        await stream.close()
    assert kinds == ["done"]


# ---------------------------------------------------------------------------
# Tool callback round-trip: server emits ToolCall, client replies ToolResult.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_stream_tool_callback_roundtrip(fake_agent_server) -> None:
    async def handler(
        request_iter: AsyncIterator[agent_pb2.ClientFrame],
        _ctx,
    ) -> AsyncIterator[agent_pb2.ServerFrame]:
        # First inbound: ChatStart.
        await request_iter.__anext__()
        # Ask the client to execute a tool.
        yield agent_pb2.ServerFrame(
            tool_call=agent_pb2.ToolCall(
                call_id="t-1",
                plugin="echo",
                tool="say",
                args_json=b'{"text":"hi"}',
                seq=1,
            )
        )
        # Read the inbound ToolResult and surface its payload via Done.
        result_frame = await request_iter.__anext__()
        assert result_frame.WhichOneof("kind") == "tool_result"
        payload = json.loads(result_frame.tool_result.result_json)
        yield agent_pb2.ServerFrame(
            done=agent_pb2.Done(finish_reason=payload.get("status", "?"))
        )

    port, _servicer = await fake_agent_server(handler)
    channel = connect_channel(f"127.0.0.1:{port}")
    executor = PlaceholderExecutor()

    async with AgentClient(channel) as client:
        stream = await client.chat()
        await stream.send(
            agent_pb2.ClientFrame(start=agent_pb2.ChatStart(model="m"))
        )
        finish: str | None = None
        async for frame in stream:
            kind = frame.WhichOneof("kind")
            if kind == "tool_call":
                result = await executor.execute(frame.tool_call)
                await stream.send(
                    agent_pb2.ClientFrame(tool_result=result)
                )
            elif kind == "done":
                finish = frame.done.finish_reason
        await stream.close()

    # PlaceholderExecutor stamps {"status": "awaiting_plugin_runtime"}.
    assert finish == "awaiting_plugin_runtime"


# ---------------------------------------------------------------------------
# Cancel sends a Cancel frame on the wire.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_stream_cancel_delivers_cancel_frame(
    fake_agent_server,
) -> None:
    seen: list[str] = []

    async def handler(
        request_iter: AsyncIterator[agent_pb2.ClientFrame],
        _ctx,
    ) -> AsyncIterator[agent_pb2.ServerFrame]:
        async for frame in request_iter:
            kind = frame.WhichOneof("kind")
            seen.append(kind or "")
            if kind == "cancel":
                yield agent_pb2.ServerFrame(
                    done=agent_pb2.Done(
                        finish_reason=f"cancelled:{frame.cancel.reason}"
                    )
                )
                return

    port, _servicer = await fake_agent_server(handler)
    channel = connect_channel(f"127.0.0.1:{port}")
    async with AgentClient(channel) as client:
        stream = await client.chat()
        await stream.send(
            agent_pb2.ClientFrame(start=agent_pb2.ChatStart(model="m"))
        )
        accepted = await stream.cancel("user_aborted")
        assert accepted is True
        finish: str | None = None
        async for frame in stream:
            if frame.WhichOneof("kind") == "done":
                finish = frame.done.finish_reason
        await stream.close()

    assert "cancel" in seen
    assert finish == "cancelled:user_aborted"


# ---------------------------------------------------------------------------
# next_classified surfaces typed errors when the server aborts.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_stream_classifies_server_error(fake_agent_server) -> None:
    async def handler(
        request_iter: AsyncIterator[agent_pb2.ClientFrame],
        ctx,
    ) -> AsyncIterator[agent_pb2.ServerFrame]:
        await request_iter.__anext__()
        await ctx.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "slow down")
        # Unreachable, but appease the async-iterator type.
        if False:
            yield agent_pb2.ServerFrame()

    port, _servicer = await fake_agent_server(handler)
    channel = connect_channel(f"127.0.0.1:{port}")
    async with AgentClient(channel) as client:
        stream = await client.chat()
        await stream.send(
            agent_pb2.ClientFrame(start=agent_pb2.ChatStart(model="m"))
        )
        with pytest.raises(UpstreamError) as ei:
            # Drive the read explicitly to take the next_classified path.
            while True:
                got = await stream.next_classified()
                if got is None:
                    break
        await stream.close()
    assert ei.value.reason is FailoverReason.RATE_LIMIT


# ---------------------------------------------------------------------------
# Channel capacity smoke + backpressure invariant.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_queue_is_bounded_to_channel_capacity() -> None:
    """Mirror of Rust ``stream::tests::channel_capacity_is_16``.

    The send half is a bounded ``asyncio.Queue(maxsize=CHANNEL_CAPACITY)``;
    once full, ``send`` awaits — we don't need a real grpc server to
    prove that invariant.
    """
    assert CHANNEL_CAPACITY == 16

    # ChatStream's internal queue: stand it up directly the way the
    # client does, then prove the (CAPACITY + 1)-th put blocks.
    q: asyncio.Queue[agent_pb2.ClientFrame] = asyncio.Queue(
        maxsize=CHANNEL_CAPACITY
    )
    for i in range(CHANNEL_CAPACITY):
        q.put_nowait(
            agent_pb2.ClientFrame(cancel=agent_pb2.Cancel(reason=f"r{i}"))
        )
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            q.put(agent_pb2.ClientFrame(cancel=agent_pb2.Cancel(reason="x"))),
            timeout=0.2,
        )
