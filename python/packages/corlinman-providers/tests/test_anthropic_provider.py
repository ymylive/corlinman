"""Anthropic provider unit tests — all offline, no network.

Strategy: monkeypatch ``anthropic.AsyncAnthropic`` with a minimal fake that
emulates the ``messages.stream()`` async context manager and its raw-event
stream. Keeps the provider behaviour under test while dodging the vendor
SDK's heavy HTTP transport.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_providers import AnthropicProvider, ProviderChunk
from corlinman_providers.anthropic_provider import _map_stop_reason, _split_system
from corlinman_providers.registry import ProviderRegistry, resolve


class _FakeStream:
    """Fake ``messages.stream()`` async context manager."""

    def __init__(self, events: list[Any], stop_reason: str = "end_turn") -> None:
        self._events = events
        self._stop_reason = stop_reason

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[Any]:
        events = self._events

        async def _gen() -> AsyncIterator[Any]:
            for e in events:
                yield e

        return _gen()

    async def get_final_message(self) -> Any:
        return SimpleNamespace(stop_reason=self._stop_reason)


class _FakeMessages:
    def __init__(self, stream: _FakeStream) -> None:
        self._stream = stream

    def stream(self, **_: Any) -> _FakeStream:
        return self._stream


class _FakeClient:
    def __init__(self, events: list[Any], stop_reason: str = "end_turn") -> None:
        self.messages = _FakeMessages(_FakeStream(events, stop_reason))


def _patch_anthropic(monkeypatch: pytest.MonkeyPatch, fake_client: _FakeClient) -> None:
    import anthropic  # type: ignore[import-not-found]

    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **_: fake_client)


def _text_event(text: str) -> Any:
    return SimpleNamespace(
        type="content_block_delta",
        index=0,
        delta=SimpleNamespace(type="text_delta", text=text),
    )


def _tool_start_event(index: int, tool_id: str, name: str) -> Any:
    return SimpleNamespace(
        type="content_block_start",
        index=index,
        content_block=SimpleNamespace(type="tool_use", id=tool_id, name=name),
    )


def _tool_delta_event(index: int, partial: str) -> Any:
    return SimpleNamespace(
        type="content_block_delta",
        index=index,
        delta=SimpleNamespace(type="input_json_delta", partial_json=partial),
    )


def _block_stop_event(index: int) -> Any:
    return SimpleNamespace(type="content_block_stop", index=index)


@pytest.mark.asyncio
async def test_no_api_key_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    prov = AnthropicProvider()
    with pytest.raises(RuntimeError, match="API key missing"):
        async for _ in prov.chat_stream(model="claude-sonnet-4-5", messages=[]):
            pass


@pytest.mark.asyncio
async def test_supports_claude_prefix() -> None:
    assert AnthropicProvider.supports("claude-sonnet-4-5")
    assert AnthropicProvider.supports("claude-3-opus")
    assert not AnthropicProvider.supports("gpt-4o")


@pytest.mark.asyncio
async def test_chat_stream_yields_text_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    events = [_text_event("hello "), _text_event("world"), _block_stop_event(0)]
    fake = _FakeClient(events)
    _patch_anthropic(monkeypatch, fake)

    prov = AnthropicProvider()
    chunks: list[ProviderChunk] = []
    async for chunk in prov.chat_stream(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
    ):
        chunks.append(chunk)

    texts = [c.text for c in chunks if c.kind == "token"]
    assert texts == ["hello ", "world"]
    assert chunks[-1].kind == "done"
    assert chunks[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_chat_stream_maps_max_tokens_to_length(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    fake = _FakeClient([_text_event("partial")], stop_reason="max_tokens")
    _patch_anthropic(monkeypatch, fake)

    prov = AnthropicProvider()
    finish: str | None = None
    async for chunk in prov.chat_stream(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
    ):
        if chunk.kind == "done":
            finish = chunk.finish_reason
    assert finish == "length"


@pytest.mark.asyncio
async def test_chat_stream_emits_tool_call_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """``tool_use`` content blocks translate into tool_call_{start,delta,end}."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    events = [
        _tool_start_event(0, "call_abc", "FooPlugin"),
        _tool_delta_event(0, '{"q":'),
        _tool_delta_event(0, '"hi"}'),
        _block_stop_event(0),
    ]
    fake = _FakeClient(events, stop_reason="tool_use")
    _patch_anthropic(monkeypatch, fake)

    prov = AnthropicProvider()
    chunks: list[ProviderChunk] = []
    async for chunk in prov.chat_stream(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "go"}],
    ):
        chunks.append(chunk)

    kinds = [c.kind for c in chunks]
    assert kinds == ["tool_call_start", "tool_call_delta", "tool_call_delta", "tool_call_end", "done"]
    assert chunks[0].tool_call_id == "call_abc"
    assert chunks[0].tool_name == "FooPlugin"
    assert chunks[1].arguments_delta == '{"q":'
    assert chunks[2].arguments_delta == '"hi"}'
    assert chunks[3].tool_call_id == "call_abc"
    assert chunks[-1].finish_reason == "tool_calls"


def test_split_system_extracts_system_and_keeps_order() -> None:
    system, chat = _split_system(
        [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "system", "content": "also terse"},
        ]
    )
    assert system == "you are helpful\n\nalso terse"
    assert [m["role"] for m in chat] == ["user", "assistant"]
    assert chat[0]["content"] == "hi"


def test_map_stop_reason_defaults_to_stop() -> None:
    assert _map_stop_reason(None) == "stop"
    assert _map_stop_reason("unknown_reason") == "stop"
    assert _map_stop_reason("tool_use") == "tool_calls"


def test_registry_resolves_claude_prefix() -> None:
    reg = ProviderRegistry()
    p = reg.resolve("claude-sonnet-4-5")
    assert p.__class__.__name__ == "AnthropicProvider"


def test_registry_raises_for_unknown() -> None:
    with pytest.raises(KeyError):
        resolve("mystery-llm-9")


def test_split_system_translates_image_url_part_to_anthropic_block() -> None:
    """OpenAI-shape ``image_url`` content part becomes Anthropic's
    ``{"type": "image", "source": {"type": "url", ...}}`` block."""
    _, chat = _split_system(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look at this"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://cdn/pic.png"},
                    },
                ],
            }
        ]
    )
    assert len(chat) == 1
    content = chat[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "look at this"}
    assert content[1] == {
        "type": "image",
        "source": {"type": "url", "url": "https://cdn/pic.png"},
    }


def test_split_system_translates_data_url_to_base64_block() -> None:
    """``data:image/png;base64,...`` URI decodes into Anthropic's base64 source."""
    _, chat = _split_system(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,iVBORw0KGgo="
                        },
                    },
                ],
            }
        ]
    )
    content = chat[0]["content"]
    assert content[0] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "iVBORw0KGgo=",
        },
    }


def test_split_system_drops_unsupported_file_part() -> None:
    """``file`` part (audio/video) is skipped with a warn — text survives."""
    _, chat = _split_system(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "transcript:"},
                    {
                        "type": "file",
                        "file": {"kind": "audio", "url": "https://x/a.amr"},
                    },
                ],
            }
        ]
    )
    content = chat[0]["content"]
    assert content == [{"type": "text", "text": "transcript:"}]


@pytest.mark.asyncio
async def test_chat_stream_with_image_url_part(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: multipart user content reaches the SDK with Anthropic
    blocks, and the stream still yields token + done chunks."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    captured: dict[str, Any] = {}

    class _CapturingMessages:
        def stream(self, **kwargs: Any) -> _FakeStream:
            captured.update(kwargs)
            return _FakeStream([_text_event("ack")], stop_reason="end_turn")

    class _CapturingClient:
        def __init__(self) -> None:
            self.messages = _CapturingMessages()

    fake = _CapturingClient()
    _patch_anthropic(monkeypatch, fake)

    prov = AnthropicProvider()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://cdn/pic.png"},
                },
            ],
        }
    ]
    tokens: list[str] = []
    async for chunk in prov.chat_stream(
        model="claude-sonnet-4-5", messages=messages
    ):
        if chunk.kind == "token":
            tokens.append(chunk.text)

    assert tokens == ["ack"]
    sent_messages = captured["messages"]
    assert len(sent_messages) == 1
    content = sent_messages[0]["content"]
    assert content[0] == {"type": "text", "text": "describe"}
    assert content[1] == {
        "type": "image",
        "source": {"type": "url", "url": "https://cdn/pic.png"},
    }
