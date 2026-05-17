"""Mock (echo) provider unit tests — fully offline, no network.

Covers the easy-setup skip path's runtime contract (Wave 2.2):

* chat: last user message is reversed, preamble banner present;
* empty history: response is just the preamble (no crash);
* streaming: at least one ``token`` chunk + terminal ``done``;
* embeddings: deterministic zero vectors at the requested dim;
* registry wiring: ``ProviderKind.MOCK`` resolves to :class:`MockProvider`.
"""

from __future__ import annotations

import pytest
from corlinman_providers import (
    MOCK_PREAMBLE,
    MockProvider,
    ProviderChunk,
    ProviderKind,
    ProviderRegistry,
    ProviderSpec,
)


@pytest.mark.asyncio
async def test_chat_reverses_last_user_message() -> None:
    """``["hello", "world"]`` → response contains preamble + reversed "world"."""
    prov = MockProvider()
    chunks: list[ProviderChunk] = []
    async for chunk in prov.chat_stream(
        model="mock",
        messages=[
            {"role": "user", "content": "hello"},
            {"role": "user", "content": "world"},
        ],
    ):
        chunks.append(chunk)

    tokens = [c for c in chunks if c.kind == "token"]
    assert len(tokens) >= 1
    text = "".join(c.text or "" for c in tokens)
    assert MOCK_PREAMBLE in text
    assert "[mock provider]" in text or MOCK_PREAMBLE in text
    # The reversed last user message lands in the body.
    assert "dlrow" in text


@pytest.mark.asyncio
async def test_chat_simple_hello_reverses_to_olleh() -> None:
    """Plan-spec example: ``"hello"`` → ``"olleh"`` in the body."""
    prov = MockProvider()
    text_chunks: list[str] = []
    async for chunk in prov.chat_stream(
        model="mock",
        messages=[{"role": "user", "content": "hello"}],
    ):
        if chunk.kind == "token" and chunk.text:
            text_chunks.append(chunk.text)

    body = "".join(text_chunks)
    assert "olleh" in body
    assert MOCK_PREAMBLE in body


@pytest.mark.asyncio
async def test_chat_empty_history_returns_preamble_only() -> None:
    """No messages → response is just the preamble (no crash)."""
    prov = MockProvider()
    chunks: list[ProviderChunk] = []
    async for chunk in prov.chat_stream(model="mock", messages=[]):
        chunks.append(chunk)

    assert chunks  # non-empty stream
    tokens = [c for c in chunks if c.kind == "token"]
    text = "".join(c.text or "" for c in tokens)
    assert MOCK_PREAMBLE in text
    # Terminal chunk is always 'done' with finish_reason="stop".
    assert chunks[-1].kind == "done"
    assert chunks[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_chat_streaming_yields_at_least_one_chunk() -> None:
    """Stream MUST yield at least one chunk before the terminal ``done``."""
    prov = MockProvider()
    chunks: list[ProviderChunk] = []
    async for chunk in prov.chat_stream(
        model="mock",
        messages=[{"role": "user", "content": "ping"}],
    ):
        chunks.append(chunk)

    non_terminal = [c for c in chunks if c.kind != "done"]
    assert len(non_terminal) >= 1
    assert chunks[-1].kind == "done"


@pytest.mark.asyncio
async def test_chat_ignores_assistant_then_uses_last_user_message() -> None:
    """Iteration order is "last user, regardless of trailing assistant"."""
    prov = MockProvider()
    text_parts: list[str] = []
    async for chunk in prov.chat_stream(
        model="mock",
        messages=[
            {"role": "user", "content": "abcdef"},
            {"role": "assistant", "content": "ignored"},
        ],
    ):
        if chunk.kind == "token" and chunk.text:
            text_parts.append(chunk.text)

    body = "".join(text_parts)
    assert "fedcba" in body
    # The assistant content must not have leaked into the reversed body.
    assert "denoring" not in body


@pytest.mark.asyncio
async def test_chat_does_not_emit_tool_calls() -> None:
    """Mock provider never generates tool calls — assistant text only."""
    prov = MockProvider()
    kinds: list[str] = []
    async for chunk in prov.chat_stream(
        model="mock",
        messages=[{"role": "user", "content": "use a tool"}],
        tools=[{"type": "function", "function": {"name": "foo"}}],
    ):
        kinds.append(chunk.kind)

    assert "tool_call_start" not in kinds
    assert "tool_call_delta" not in kinds
    assert "tool_call_end" not in kinds


@pytest.mark.asyncio
async def test_chat_handles_multipart_content_parts() -> None:
    """OpenAI-shape ``[{type:text, text:...}]`` content collapses to text."""
    prov = MockProvider()
    text_parts: list[str] = []
    async for chunk in prov.chat_stream(
        model="mock",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "abc"},
                    {"type": "text", "text": "def"},
                ],
            },
        ],
    ):
        if chunk.kind == "token" and chunk.text:
            text_parts.append(chunk.text)

    body = "".join(text_parts)
    # "abcdef" reversed -> "fedcba"
    assert "fedcba" in body


@pytest.mark.asyncio
async def test_embed_returns_zero_vectors_at_requested_dim() -> None:
    """Embedding API returns one zero-vector per input at the requested dim."""
    prov = MockProvider()
    out = await prov.embed(
        model="mock",
        inputs=["one", "two", "three"],
        extra={"dimension": 8},
    )
    assert len(out) == 3
    for vec in out:
        assert len(vec) == 8
        assert all(component == 0.0 for component in vec)


@pytest.mark.asyncio
async def test_embed_defaults_to_3072_when_no_dim_specified() -> None:
    """Caller-supplied dim is optional — default matches RAG pipeline (3072)."""
    prov = MockProvider()
    out = await prov.embed(model="mock", inputs=["solo"])
    assert len(out) == 1
    assert len(out[0]) == 3072
    assert all(component == 0.0 for component in out[0])


def test_supports_only_mock_family() -> None:
    """``supports()`` is conservative: only ``mock`` / ``mock-*`` match."""
    assert MockProvider.supports("mock") is True
    assert MockProvider.supports("mock-echo") is True
    assert MockProvider.supports("gpt-4o") is False
    assert MockProvider.supports("claude-opus-4") is False


def test_registry_builds_mock_kind() -> None:
    """``ProviderKind.MOCK`` resolves through the registry without creds."""
    spec = ProviderSpec(
        name="mock",
        kind=ProviderKind.MOCK,
        api_key=None,
        base_url=None,
        enabled=True,
        params={},
    )
    reg = ProviderRegistry([spec])
    built = reg.get("mock")
    assert built is not None
    assert isinstance(built, MockProvider)


def test_registry_resolve_via_alias_to_mock() -> None:
    """Default-alias path: model name "mock" → MockProvider."""
    from corlinman_providers import AliasEntry

    spec = ProviderSpec(
        name="mock",
        kind=ProviderKind.MOCK,
        enabled=True,
        params={},
    )
    reg = ProviderRegistry([spec])
    aliases = {"mock": AliasEntry(provider="mock", model="mock", params={})}
    provider, model, _ = reg.resolve("mock", aliases=aliases)
    assert isinstance(provider, MockProvider)
    assert model == "mock"


def test_mock_params_schema_is_empty_object() -> None:
    """No tunable knobs — schema validates only ``{}``."""
    schema = MockProvider.params_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["properties"] == {}
