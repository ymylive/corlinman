"""Shared fixtures for ``agent_client`` tests.

The :func:`fake_agent_server` fixture spins a real ``grpc.aio.server``
hosting a configurable :class:`FakeAgentServicer`, returning the
listen port. Mirrors the in-process server pattern already used in
``corlinman-server``'s tests so the two suites read consistently.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any

import grpc
import grpc.aio
import pytest_asyncio
from corlinman_grpc._generated.corlinman.v1 import (
    agent_pb2,
    agent_pb2_grpc,
)

# Type of a behaviour callback: receives the inbound iterator + context,
# yields ServerFrames (or raises grpc errors).
ChatHandler = Callable[
    [AsyncIterator[agent_pb2.ClientFrame], grpc.aio.ServicerContext],
    AsyncIterator[agent_pb2.ServerFrame],
]


class FakeAgentServicer(agent_pb2_grpc.AgentServicer):
    """Configurable in-process servicer.

    Pass a ``handler(request_iter, context) -> async iterator`` to
    drive frame-level behaviour, or rely on the default which echoes
    a single ``Done(finish_reason="stop")`` after consuming the first
    inbound frame.
    """

    def __init__(self, handler: ChatHandler | None = None) -> None:
        self._handler = handler
        self.received_metadata: list[tuple[str, str]] = []

    async def Chat(  # noqa: N802 — gRPC contract
        self,
        request_iterator: AsyncIterator[agent_pb2.ClientFrame],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[agent_pb2.ServerFrame]:
        # Record metadata for trace-propagation assertions.
        try:
            self.received_metadata = [
                (k, v) for k, v in context.invocation_metadata() or ()
            ]
        except Exception:  # pragma: no cover — defensive
            self.received_metadata = []

        if self._handler is None:
            # Default: drain one frame, return Done.
            with suppress(StopAsyncIteration):
                await request_iterator.__anext__()
            yield agent_pb2.ServerFrame(
                done=agent_pb2.Done(finish_reason="stop")
            )
            return

        async for frame in self._handler(request_iterator, context):
            yield frame


@asynccontextmanager
async def _serve(
    servicer: FakeAgentServicer,
) -> AsyncIterator[int]:
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield port
    finally:
        await server.stop(grace=None)


@pytest_asyncio.fixture
async def fake_agent_server() -> AsyncIterator[
    Callable[[ChatHandler | None], Awaitable[tuple[int, FakeAgentServicer]]]
]:
    """Factory fixture: ``port, servicer = await fake_agent_server(handler)``.

    Multiple servers can be created in the same test; they all tear
    down at fixture exit.
    """
    stacks: list[Any] = []

    async def start(handler: ChatHandler | None = None) -> tuple[int, FakeAgentServicer]:
        servicer = FakeAgentServicer(handler)
        ctx = _serve(servicer)
        port = await ctx.__aenter__()
        stacks.append(ctx)
        return port, servicer

    try:
        yield start
    finally:
        for ctx in reversed(stacks):
            await ctx.__aexit__(None, None, None)
