"""Full-stack servicer test: real gRPC server, fake provider, verify frames."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
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


class _FakeContextAssembler:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def assemble(
        self,
        messages: list[dict[str, Any]],
        *,
        session_key: str,
        model_name: str,
        metadata: dict[str, str] | None = None,
    ) -> Any:
        self.calls.append(
            {
                "messages": messages,
                "session_key": session_key,
                "model_name": model_name,
                "metadata": dict(metadata or {}),
            }
        )
        rendered = [dict(m) for m in messages]
        for msg in rendered:
            if msg.get("role") == "system" and isinstance(msg.get("content"), str):
                msg["content"] = msg["content"].replace(
                    "{{memory.backend}}", "memory hit from assembler"
                )
        return SimpleNamespace(messages=rendered)


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
async def test_servicer_assembles_context_before_provider_call() -> None:
    fake = _FakeProvider(_token_stream(["ok"]))
    assembler = _FakeContextAssembler()

    def _resolver(_model: str) -> Any:
        return fake

    servicer = CorlinmanAgentServicer(
        provider_resolver=_resolver,
        context_assembler=assembler,
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
                    start=agent_pb2.ChatStart(
                        model="gpt-4o-mini",
                        session_key="sess-ctx",
                        messages=[
                            common_pb2.Message(
                                role=common_pb2.SYSTEM,
                                content="Recall: {{memory.backend}}",
                            ),
                            common_pb2.Message(role=common_pb2.USER, content="hi"),
                        ],
                    )
                )

            call = stub.Chat(frames())
            async for _ in call:
                pass
    finally:
        await server.stop(grace=None)

    assert assembler.calls
    assert assembler.calls[0]["session_key"] == "sess-ctx"
    assert assembler.calls[0]["model_name"] == "gpt-4o-mini"
    provider_messages = fake.last_kwargs["messages"]
    assert provider_messages[0]["content"] == "Recall: memory hit from assembler"


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


# ---------------------------------------------------------------------------
# v0.7 multi-agent: builtin tool interception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_servicer_dispatches_blackboard_write_in_process(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The servicer's ``_dispatch_builtin`` handles ``blackboard.write``
    without going through the gateway plugin runtime. We exercise the
    method directly because the streaming-loop test fixture is
    quadratic to set up — the unit-level contract here is enough."""
    from corlinman_agent.reasoning_loop import ChatStart, ToolCallEvent
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    # Point the lazy data dir at an isolated tmp so the test never
    # writes outside its sandbox.
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    start = ChatStart(
        model="m",
        messages=[],
        tools=[],
        session_key="tenant-x::session-1",
    )
    event = ToolCallEvent(
        call_id="c1",
        plugin="blackboard",
        tool="blackboard.write",
        args_json=b'{"key": "topic", "value": "research the moon"}',
    )

    result_json = await servicer._dispatch_builtin(
        event, start, _FakeProvider([])
    )
    payload = json.loads(result_json)
    assert payload["key"] == "topic"
    assert "error" not in payload
    # Receipt: an int written_at + the parent agent id as written_by.
    assert isinstance(payload["written_at"], int)
    assert "agent" in payload["written_by"] or payload["written_by"] == "m"

    # Read it back via the same method to lock the round-trip.
    read_event = ToolCallEvent(
        call_id="c2",
        plugin="blackboard",
        tool="blackboard.read",
        args_json=b'{"key": "topic"}',
    )
    read_json = await servicer._dispatch_builtin(
        read_event, start, _FakeProvider([])
    )
    read_payload = json.loads(read_json)
    assert read_payload == {
        "key": "topic",
        "value": "research the moon",
        "present": True,
    }


@pytest.mark.asyncio
async def test_servicer_builtin_tool_unknown_envelope() -> None:
    """An unrecognised tool name returns a structured error envelope
    rather than raising — the model's next round still has something
    to read."""
    from corlinman_agent.reasoning_loop import ChatStart, ToolCallEvent
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    start = ChatStart(model="m", messages=[], tools=[], session_key="s")
    event = ToolCallEvent(
        call_id="c",
        plugin="x",
        tool="blackboard.unknown",
        args_json=b"{}",
    )
    # This tool isn't in BUILTIN_TOOLS so the loop wouldn't normally
    # call _dispatch_builtin, but the method itself is defensive.
    result_json = await servicer._dispatch_builtin(
        event, start, _FakeProvider([])
    )
    payload = json.loads(result_json)
    assert "error" in payload


@pytest.mark.asyncio
async def test_servicer_dispatches_spawn_many_round_trip(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end smoke for v0.7 fan-out: a spawn_many ToolCallEvent
    flows through _dispatch_builtin, dispatches two siblings, and
    returns a ``{"tasks": [TaskResult, TaskResult]}`` envelope.

    Uses a stateful agent registry pre-populated with `researcher` and
    `editor` so the per-sibling dispatch can resolve the child cards.
    """
    from corlinman_agent.agents.card import AgentCard
    from corlinman_agent.agents.registry import AgentCardRegistry
    from corlinman_agent.reasoning_loop import ChatStart, ToolCallEvent
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    # Inject the agent registry directly; the lazy loader would otherwise
    # try to read `agents/*.yaml` from CORLINMAN_DATA_DIR which is empty here.
    servicer._builtin_agents = AgentCardRegistry(
        {
            "researcher": AgentCard(
                name="researcher", description="", system_prompt="you research"
            ),
            "editor": AgentCard(
                name="editor", description="", system_prompt="you edit"
            ),
        }
    )

    # Per-sibling provider: each child gets one chat_stream call that
    # streams a single token + done(stop). The same instance is shared
    # across siblings because the dispatch path doesn't care.
    provider = _FakeProvider(_token_stream(["did the work"]))

    start = ChatStart(
        model="orchestrator",
        messages=[],
        tools=[],
        session_key="tenant-a::sess-1",
    )
    args = json.dumps(
        {
            "tasks": [
                {"agent": "researcher", "goal": "find papers on X"},
                {"agent": "editor", "goal": "tighten the prose"},
            ]
        }
    )
    event = ToolCallEvent(
        call_id="spawn-1",
        plugin="subagent",
        tool="subagent.spawn_many",
        args_json=args.encode(),
    )
    result_json = await servicer._dispatch_builtin(event, start, provider)
    payload = json.loads(result_json)
    assert "error" not in payload, "fan-out happy path must elide outer error"
    assert len(payload["tasks"]) == 2
    # Order preserved from input.
    assert payload["tasks"][0]["child_session_key"].endswith("::child::0")
    assert payload["tasks"][1]["child_session_key"].endswith("::child::1")
    # Both stopped cleanly.
    for sibling in payload["tasks"]:
        assert sibling["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_servicer_threads_parent_tools_into_spawn(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``start.tools`` (the parent's full tool schema list) must reach
    the per-sibling dispatch so the child's allowlist filter can
    intersect against the parent's set. The contract is "child cannot
    request a tool the parent doesn't hold". This locks the wiring
    at the servicer boundary so a regression that drops ``start.tools``
    on the floor would surface here, not on a live deployment."""
    from corlinman_agent.agents.card import AgentCard
    from corlinman_agent.agents.registry import AgentCardRegistry
    from corlinman_agent.reasoning_loop import ChatStart, ToolCallEvent
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    servicer._builtin_agents = AgentCardRegistry(
        {"researcher": AgentCard(name="researcher", description="", system_prompt="r")}
    )

    parent_tools = [
        {
            "type": "function",
            "function": {
                "name": "web.search",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    start = ChatStart(
        model="m",
        messages=[],
        tools=parent_tools,
        session_key="s",
    )
    # The child requests a tool the parent does NOT hold ("file.read").
    # If start.tools threads through correctly, the runner's allowlist
    # filter rejects the spawn with ``tool_allowlist_escalation``.
    args = json.dumps(
        {
            "agent": "researcher",
            "goal": "x",
            "tool_allowlist": ["file.read"],
        }
    )
    event = ToolCallEvent(
        call_id="c",
        plugin="subagent",
        tool="subagent.spawn",
        args_json=args.encode(),
    )
    result_json = await servicer._dispatch_builtin(
        event, start, _FakeProvider([])
    )
    payload = json.loads(result_json)
    # The escalation reject proves parent_tools flowed through; if
    # ``start.tools`` had been dropped, the filter would have seen an
    # empty parent set and the child would have just inherited (silent
    # success with no tools).
    assert payload["finish_reason"] == "rejected"
    assert "tool_allowlist_escalation" in payload["error"]


# ---------------------------------------------------------------------------
# v0.7.1 warm pool: prewarm_providers surface
# ---------------------------------------------------------------------------


def test_servicer_prewarm_providers_populates_pool() -> None:
    """``prewarm_providers`` resolves each model name via the
    configured resolver and parks the result in the pool. We assert
    on the pool stats since the resolver's return value is opaque
    to the servicer's hot path."""
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    resolved_calls: list[str] = []

    def resolver(model: str) -> Any:
        resolved_calls.append(model)
        return _FakeProvider(_token_stream(["ok"]))

    servicer = CorlinmanAgentServicer(provider_resolver=resolver)
    servicer.prewarm_providers(["alpha", "beta", "gamma"])

    # All three resolutions happened at boot, not at first chat.
    assert resolved_calls == ["alpha", "beta", "gamma"]
    s = servicer.pool_stats()
    assert s.warm_count == 3
    assert s.misses == 0  # prewarm does not count as a miss


def test_servicer_prewarm_swallows_resolution_errors() -> None:
    """An unresolved alias must not crash the boot — the failed entry
    is skipped, others succeed."""
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    def resolver(model: str) -> Any:
        if model == "bad":
            raise KeyError(model)
        return _FakeProvider([])

    servicer = CorlinmanAgentServicer(provider_resolver=resolver)
    servicer.prewarm_providers(["good", "bad", "also-good"])
    s = servicer.pool_stats()
    # Only the two good ones landed warm.
    assert s.warm_count == 2
