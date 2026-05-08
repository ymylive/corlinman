"""Full-stack servicer test: real gRPC server, fake provider, verify frames."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import grpc
import grpc.aio
import pytest
from corlinman_grpc import agent_pb2, agent_pb2_grpc, common_pb2
from corlinman_providers import AliasEntry, ProviderRegistry
from corlinman_providers.base import ProviderChunk
from corlinman_server.agent_servicer import CorlinmanAgentServicer


class _FakeProvider:
    """Yields a pre-recorded ``ProviderChunk`` sequence.

    Records the kwargs passed to ``chat_stream`` on ``last_kwargs`` so
    tests can assert that merged params flow through.
    """

    def __init__(self, chunks: list[ProviderChunk]) -> None:
        self._chunks = chunks
        self.last_kwargs: dict[str, Any] = {}

    async def chat_stream(self, **kwargs: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        self.last_kwargs = kwargs
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


@pytest.mark.asyncio
async def test_servicer_threads_merged_params_into_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature C: alias params flow through to the provider's ``chat_stream``.

    Configure a registry with one provider (default ``temperature``) and one
    alias that overrides ``temperature`` + adds ``top_p``. The servicer
    should call the provider with the alias's ``temperature`` and with
    ``top_p`` threaded through ``extra``, and with the **upstream** model
    id rather than the alias name.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    fake = _FakeProvider(_token_stream(["ok"]))

    def _resolver(
        alias_or_model: str, aliases: Any = None
    ) -> tuple[_FakeProvider, str, dict[str, Any]]:
        # Stand in for ProviderRegistry.resolve — we don't need a real one
        # for this test, we just need the servicer to use whatever we return.
        assert alias_or_model == "fast-chat"
        return fake, "gpt-4o-mini", {"temperature": 0.9, "top_p": 0.95}

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
                    start=agent_pb2.ChatStart(model="fast-chat")
                )

            call = stub.Chat(frames())
            async for _ in call:
                pass
    finally:
        await server.stop(grace=None)

    # Provider was called with upstream model id, merged temperature, and
    # the remaining param flowed through via ``extra``.
    assert fake.last_kwargs["model"] == "gpt-4o-mini"
    assert fake.last_kwargs["temperature"] == pytest.approx(0.9)
    extra = fake.last_kwargs.get("extra") or {}
    assert extra.get("top_p") == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_servicer_forwards_openai_tools_json_to_provider() -> None:
    """Client-supplied OpenAI tools must reach the provider call."""
    fake = _FakeProvider(_token_stream(["ok"]))
    tools = [
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "Search docs",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
    ]

    def _resolver(_model: str) -> Any:
        return fake

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
                        model="gpt-4o-mini",
                        tools_json=json.dumps(tools).encode("utf-8"),
                    )
                )

            call = stub.Chat(frames())
            async for _ in call:
                pass
    finally:
        await server.stop(grace=None)

    assert fake.last_kwargs["tools"] == tools


@pytest.mark.asyncio
async def test_servicer_registry_end_to_end_resolves_alias() -> None:
    """Wire a real ``ProviderRegistry`` + alias map through the servicer."""
    from corlinman_providers import ProviderKind, ProviderSpec

    fake = _FakeProvider(_token_stream(["ok"]))
    # Build a registry with one pretend-spec, then swap the built adapter
    # for our fake so we can inspect call args without hitting the SDK.
    spec = ProviderSpec(
        name="oai",
        kind=ProviderKind.OPENAI,
        api_key="sk-test",
        params={"temperature": 0.2},
    )
    reg = ProviderRegistry([spec])
    reg._providers["oai"] = fake  # type: ignore[assignment]  # test-only override
    aliases = {
        "creative": AliasEntry(
            provider="oai",
            model="gpt-4o",
            params={"temperature": 1.3},
        )
    }

    servicer = CorlinmanAgentServicer(
        provider_resolver=reg.resolve, aliases=aliases
    )
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = agent_pb2_grpc.AgentStub(channel)

            async def frames():
                yield agent_pb2.ClientFrame(
                    start=agent_pb2.ChatStart(model="creative")
                )

            call = stub.Chat(frames())
            async for _ in call:
                pass
    finally:
        await server.stop(grace=None)

    assert fake.last_kwargs["model"] == "gpt-4o"
    # alias.temperature (1.3) wins over provider.temperature (0.2).
    assert fake.last_kwargs["temperature"] == pytest.approx(1.3)
