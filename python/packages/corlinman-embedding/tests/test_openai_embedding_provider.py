"""OpenAI-compatible embedding provider — offline, fake ``AsyncOpenAI``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_embedding import OpenAICompatibleEmbeddingProvider


class _FakeEmbeddings:
    def __init__(self, payload: list[list[float]]) -> None:
        self._payload = payload
        self.last_kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=v) for v in self._payload],
        )


class _FakeClient:
    def __init__(self, payload: list[list[float]]) -> None:
        self.embeddings = _FakeEmbeddings(payload)


def _patch_openai(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    import openai  # type: ignore[import-not-found]

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **_: client)


@pytest.mark.asyncio
async def test_embed_returns_vectors_and_forwards_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    client = _FakeClient([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])
    _patch_openai(monkeypatch, client)

    provider = OpenAICompatibleEmbeddingProvider(
        model="text-embedding-3-small",
        api_key="test",
        base_url="http://localhost/v1",
    )
    vectors = await provider.embed(["hello", "world"], dimension=3)

    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    # The configured dimension is forwarded as ``dimensions=`` to the SDK.
    assert client.embeddings.last_kwargs["dimensions"] == 3
    assert client.embeddings.last_kwargs["input"] == ["hello", "world"]


@pytest.mark.asyncio
async def test_embed_missing_api_key_raises() -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        model="m", api_key=None, base_url=None
    )
    with pytest.raises(RuntimeError, match="API key"):
        await provider.embed(["x"], dimension=1)


@pytest.mark.asyncio
async def test_embed_forwards_user_param_but_drops_timeout_ms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    client = _FakeClient([[1.0]])
    _patch_openai(monkeypatch, client)

    provider = OpenAICompatibleEmbeddingProvider(
        model="text-embedding-3-small",
        api_key="test",
        base_url=None,
    )
    await provider.embed(
        ["x"], dimension=1, params={"user": "alice", "timeout_ms": 5000}
    )

    kw = client.embeddings.last_kwargs
    assert kw["user"] == "alice"
    assert "timeout_ms" not in kw  # client-side concern, not request body
