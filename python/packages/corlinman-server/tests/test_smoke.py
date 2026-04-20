"""Smoke tests — graceful shutdown primitives + end-to-end server boot.

``test_server_boots_and_returns_unimplemented`` spins up a real ``grpc.aio``
server in-process (no servicers registered beyond the default
``AgentServicer`` placeholder), opens a client channel, invokes ``Agent.Chat``,
and asserts that the server responds with ``UNIMPLEMENTED`` — proving the
server is actually serving requests while still being a clean-room boot with
no business logic attached.
"""

from __future__ import annotations

import asyncio

import grpc
import grpc.aio
import pytest
from corlinman_grpc import agent_pb2, agent_pb2_grpc
from corlinman_server import main as _main  # noqa: F401 — import check
from corlinman_server.shutdown import GracefulShutdown


@pytest.mark.asyncio
async def test_shutdown_event_resolves_with_reason() -> None:
    s = GracefulShutdown()

    async def trigger() -> None:
        await asyncio.sleep(0)
        s.request("SIGTERM")

    _trigger_task = asyncio.create_task(trigger())  # noqa: RUF006 — test-scope local ref; task completes via s.wait()
    reason = await asyncio.wait_for(s.wait(), timeout=1.0)
    assert reason == "SIGTERM"


@pytest.mark.asyncio
async def test_shutdown_is_idempotent() -> None:
    s = GracefulShutdown()
    s.request("SIGTERM")
    s.request("SIGINT")  # ignored, first caller wins
    reason = await asyncio.wait_for(s.wait(), timeout=1.0)
    assert reason == "SIGTERM"


@pytest.mark.asyncio
async def test_server_boots_with_default_servicer_rejects_unknown_model() -> None:
    """Boot the real Agent servicer, invoke Agent.Chat with an unsupported
    model id, and expect an ErrorInfo frame instead of tonic UNIMPLEMENTED."""
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentServicer_to_server(CorlinmanAgentServicer(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = agent_pb2_grpc.AgentStub(channel)

            async def one_frame():
                yield agent_pb2.ClientFrame(start=agent_pb2.ChatStart(model="nosuch-42"))

            call = stub.Chat(one_frame())
            frames: list[agent_pb2.ServerFrame] = []
            async for f in call:
                frames.append(f)
            # We expect a single ErrorInfo for unknown-model.
            assert len(frames) == 1
            assert frames[0].WhichOneof("kind") == "error"
    finally:
        await server.stop(grace=None)
