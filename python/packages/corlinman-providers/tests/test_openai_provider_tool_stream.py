"""OpenAI provider tool-stream tests — offline, monkeypatching ``AsyncOpenAI``.

The OpenAI SDK exposes chat-completion streams as an async iterator of
``ChatCompletionChunk`` objects. We fake those with ``SimpleNamespace``
shapes — duck-typing is good enough because the provider only reads
``chunk.choices[0].{delta,finish_reason}`` and within ``delta`` only
``content`` + ``tool_calls[].{index,id,function.{name,arguments}}``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_providers import OpenAIProvider, ProviderChunk


def _delta_text_chunk(text: str) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=None),
                finish_reason=None,
            )
        ]
    )


def _delta_tool_chunk(
    *, index: int, tc_id: str | None = None, name: str | None = None, arguments: str | None = None
) -> Any:
    td = SimpleNamespace(
        index=index,
        id=tc_id,
        function=SimpleNamespace(name=name, arguments=arguments) if (name or arguments) else None,
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=None, tool_calls=[td]),
                finish_reason=None,
            )
        ]
    )


def _finish_chunk(reason: str) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=None, tool_calls=None),
                finish_reason=reason,
            )
        ]
    )


class _FakeAsyncIter:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def __aiter__(self) -> AsyncIterator[Any]:
        items = self._items

        async def _gen() -> AsyncIterator[Any]:
            for it in items:
                yield it

        return _gen()


class _FakeCompletions:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    async def create(self, **_: Any) -> _FakeAsyncIter:
        return _FakeAsyncIter(self._chunks)


class _FakeChat:
    def __init__(self, chunks: list[Any]) -> None:
        self.completions = _FakeCompletions(chunks)


class _FakeOpenAI:
    def __init__(self, chunks: list[Any]) -> None:
        self.chat = _FakeChat(chunks)


def _patch_openai(monkeypatch: pytest.MonkeyPatch, chunks: list[Any]) -> None:
    import openai  # type: ignore[import-not-found]

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **_: _FakeOpenAI(chunks))


@pytest.mark.asyncio
async def test_no_api_key_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    prov = OpenAIProvider()
    with pytest.raises(RuntimeError, match="API key missing"):
        async for _ in prov.chat_stream(model="gpt-4o-mini", messages=[]):
            pass


@pytest.mark.asyncio
async def test_pure_text_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _patch_openai(
        monkeypatch,
        [
            _delta_text_chunk("hi "),
            _delta_text_chunk("there"),
            _finish_chunk("stop"),
        ],
    )
    prov = OpenAIProvider()
    chunks: list[ProviderChunk] = []
    async for c in prov.chat_stream(model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]):
        chunks.append(c)

    texts = [c.text for c in chunks if c.kind == "token"]
    assert texts == ["hi ", "there"]
    assert chunks[-1].kind == "done"
    assert chunks[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_single_tool_call_aggregates_to_standard_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matches the canonical OpenAI streaming shape: first delta carries
    id+name with an empty arguments prefix, subsequent deltas append JSON
    fragments, then ``finish_reason="tool_calls"`` arrives."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _patch_openai(
        monkeypatch,
        [
            _delta_tool_chunk(index=0, tc_id="call_abc", name="FooPlugin", arguments=""),
            _delta_tool_chunk(index=0, arguments='{"q":'),
            _delta_tool_chunk(index=0, arguments='"hi"}'),
            _finish_chunk("tool_calls"),
        ],
    )
    prov = OpenAIProvider()
    chunks: list[ProviderChunk] = []
    async for c in prov.chat_stream(model="gpt-4o-mini", messages=[{"role": "user", "content": "go"}]):
        chunks.append(c)

    kinds = [c.kind for c in chunks]
    # start → delta (empty initial args are skipped) → delta → delta → end → done
    assert kinds[0] == "tool_call_start"
    assert chunks[0].tool_call_id == "call_abc"
    assert chunks[0].tool_name == "FooPlugin"

    deltas = [c.arguments_delta for c in chunks if c.kind == "tool_call_delta"]
    assert deltas == ['{"q":', '"hi"}']

    end = [c for c in chunks if c.kind == "tool_call_end"]
    assert len(end) == 1 and end[0].tool_call_id == "call_abc"

    assert chunks[-1].kind == "done"
    assert chunks[-1].finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_parallel_tool_calls_by_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two concurrent tool calls — indices 0 and 1 — must stay separate."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _patch_openai(
        monkeypatch,
        [
            _delta_tool_chunk(index=0, tc_id="c0", name="A", arguments=""),
            _delta_tool_chunk(index=1, tc_id="c1", name="B", arguments=""),
            _delta_tool_chunk(index=0, arguments="{}"),
            _delta_tool_chunk(index=1, arguments="{}"),
            _finish_chunk("tool_calls"),
        ],
    )
    prov = OpenAIProvider()
    chunks: list[ProviderChunk] = []
    async for c in prov.chat_stream(model="gpt-4o-mini", messages=[{"role": "user", "content": "go"}]):
        chunks.append(c)

    starts = [c for c in chunks if c.kind == "tool_call_start"]
    ends = [c for c in chunks if c.kind == "tool_call_end"]
    assert [s.tool_call_id for s in starts] == ["c0", "c1"]
    # Both calls must be closed before the terminal done.
    assert sorted(e.tool_call_id for e in ends) == ["c0", "c1"]
    assert chunks[-1].finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_supports_matches_gpt_and_o_series() -> None:
    assert OpenAIProvider.supports("gpt-4o-mini")
    assert OpenAIProvider.supports("o1-mini")
    assert OpenAIProvider.supports("o3-mini")
    assert not OpenAIProvider.supports("claude-sonnet-4-5")
