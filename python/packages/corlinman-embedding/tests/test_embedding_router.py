"""Tests for embedding router, remote HTTP client, and local pool."""

from __future__ import annotations

import json

import httpx
import pytest
from corlinman_embedding.local_pool import LocalEmbeddingPool
from corlinman_embedding.remote_client import RemoteEmbeddingClient
from corlinman_embedding.router import EmbeddingConfig, EmbeddingRouter


def _mock_client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


@pytest.mark.asyncio
async def test_remote_posts_openai_embedding_body_and_parses_vectors() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.3, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ],
            },
        )

    client = _mock_client(httpx.MockTransport(handler))
    try:
        provider = RemoteEmbeddingClient(
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model="embed-mini",
            client=client,
        )

        vectors = await provider.embed(["hello", "world"], dimension=2)

        assert vectors == [[0.1, 0.2], [0.3, 0.4]]
        assert captured["url"] == "https://api.example.com/v1/embeddings"
        assert captured["auth"] == "Bearer sk-test"
        body = captured["body"]
        assert isinstance(body, dict)
        assert body == {
            "model": "embed-mini",
            "input": ["hello", "world"],
            "dimensions": 2,
            "encoding_format": "float",
        }
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_local_pool_uses_injected_encoder_without_sentence_transformers() -> None:
    class FakeEncoder:
        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[float(len(text)), float(idx)] for idx, text in enumerate(texts)]

    pool = LocalEmbeddingPool(model_name="fake-local", encoder=FakeEncoder())

    vectors = await pool.embed(["a", "abcd"], dimension=2)

    assert vectors == [[1.0, 0.0], [4.0, 1.0]]


@pytest.mark.asyncio
async def test_router_selects_remote_client_and_asserts_dimension() -> None:
    class FakeRemote:
        async def embed(
            self,
            texts: list[str],
            *,
            dimension: int,
            params: dict[str, object] | None = None,
        ) -> list[list[float]]:
            assert texts == ["q"]
            assert dimension == 2
            assert params == {"user": "u1"}
            return [[0.5, 0.25]]

    router = EmbeddingRouter(
        EmbeddingConfig(source="remote", model="embed-mini", dimension=2),
        remote_client=FakeRemote(),
    )

    assert await router.embed(["q"], params={"user": "u1"}) == [[0.5, 0.25]]


@pytest.mark.asyncio
async def test_router_rejects_wrong_dimension_from_backend() -> None:
    class BadLocal:
        async def embed(
            self,
            texts: list[str],
            *,
            dimension: int,
            params: dict[str, object] | None = None,
        ) -> list[list[float]]:
            return [[1.0]]

    router = EmbeddingRouter(
        EmbeddingConfig(source="local", model="fake-local", dimension=2),
        local_pool=BadLocal(),
    )

    with pytest.raises(ValueError, match="expected dimension 2"):
        await router.embed(["q"])
