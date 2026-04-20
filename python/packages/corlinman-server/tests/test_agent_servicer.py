"""Full-stack servicer test: real gRPC server, fake provider, verify frames."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import grpc
import grpc.aio
import pytest
from corlinman_grpc import agent_pb2, agent_pb2_grpc, common_pb2
from corlinman_providers.base import ProviderChunk
from corlinman_server.agent_servicer import CorlinmanAgentServicer


class _FakeProvider:
    """Yields a pre-recorded ``ProviderChunk`` sequence."""

    def __init__(self, chunks: list[ProviderChunk]) -> None:
        self._chunks = chunks

    async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        for c in self._chunks:
            yield c


def _token_stream(deltas: list[str]) -> list[ProviderChunk]:
    """Helper: token chunks + final ``done``."""
    chunks: list[ProviderChunk] = [ProviderChunk(kind="token", text=d) for d in deltas]
    chunks.append(ProviderChunk(kind="done", finish_reason="stop"))
    return chunks


@pytest.mark.asyncio
async def test_servicer_streams_tokens_and_done() -> None:
    def _resolver(_model: str) -> Any:
        return _FakeProvider(_token_stream(["hello ", "world"]))

    servicer = CorlinmanAgentServicer(provider_resolver=_resolver)

    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = agent_pb2_grpc.AgentStub(channel)

            async def frames():
                yield agent_pb2.ClientFrame(
                    start=agent_pb2.ChatStart(
                        model="claude-sonnet-4-5",
                        messages=[
                            common_pb2.Message(role=common_pb2.USER, content="hi")
                        ],
                    )
                )

            call = stub.Chat(frames())
            received: list[str] = []
            kinds: list[str] = []
            async for f in call:
                kinds.append(f.WhichOneof("kind"))
                if f.WhichOneof("kind") == "token":
                    received.append(f.token.text)
            assert "".join(received) == "hello world"
            assert kinds[-1] == "done"
    finally:
        await server.stop(grace=None)


@pytest.mark.asyncio
async def test_env_mock_provider_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CORLINMAN_TEST_MOCK_PROVIDER`` activates the offline provider."""
    monkeypatch.setenv("CORLINMAN_TEST_MOCK_PROVIDER", "mock-delta")
    servicer = CorlinmanAgentServicer()
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = agent_pb2_grpc.AgentStub(channel)

            async def frames():
                yield agent_pb2.ClientFrame(
                    start=agent_pb2.ChatStart(model="any-model")
                )

            call = stub.Chat(frames())
            texts: list[str] = []
            async for f in call:
                if f.WhichOneof("kind") == "token":
                    texts.append(f.token.text)
            assert "".join(texts) == "mock-delta"
    finally:
        await server.stop(grace=None)


@pytest.mark.asyncio
async def test_servicer_emits_tool_call_frame() -> None:
    """Provider emits OpenAI-standard tool_call chunks → servicer yields ToolCall frame."""
    chunks = [
        ProviderChunk(
            kind="tool_call_start",
            tool_call_id="call_1",
            tool_name="foo.greet",
        ),
        ProviderChunk(
            kind="tool_call_delta",
            tool_call_id="call_1",
            arguments_delta='{"name":',
        ),
        ProviderChunk(
            kind="tool_call_delta",
            tool_call_id="call_1",
            arguments_delta='"Ada"}',
        ),
        ProviderChunk(kind="tool_call_end", tool_call_id="call_1"),
        ProviderChunk(kind="done", finish_reason="tool_calls"),
    ]

    def _resolver(_model: str) -> Any:
        return _FakeProvider(chunks)

    servicer = CorlinmanAgentServicer(provider_resolver=_resolver)
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = agent_pb2_grpc.AgentStub(channel)

            async def frames():
                yield agent_pb2.ClientFrame(
                    start=agent_pb2.ChatStart(model="claude-sonnet-4-5")
                )

            call = stub.Chat(frames())
            tool_names: list[str] = []
            async for f in call:
                if f.WhichOneof("kind") == "tool_call":
                    tool_names.append(f.tool_call.tool)
            # tool_name "foo.greet" → ToolCall.tool = "greet" (plugin/tool split) or full string;
            # the servicer layer is free to split on ".", we accept either form.
            assert tool_names and any("greet" in n for n in tool_names)
    finally:
        await server.stop(grace=None)
