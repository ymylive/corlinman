"""Tests for :mod:`corlinman_embedding.rerank_client`.

Local path is exercised only when ``sentence-transformers`` is installed
(via ``pytest.importorskip``). Remote path uses an ``httpx.MockTransport``
to avoid network I/O and a new test dependency.
"""

from __future__ import annotations

import json

import httpx
import pytest
from corlinman_embedding.rerank_client import (
    LocalRerankProvider,
    RemoteRerankProvider,
    RerankHit,
    RerankProvider,
)

# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_both_providers_satisfy_protocol() -> None:
    local = LocalRerankProvider(model_name="fake")
    remote = RemoteRerankProvider(
        base_url="http://x", api_key="k", model="rerank-mini"
    )
    # `runtime_checkable` Protocol: structural check.
    assert isinstance(local, RerankProvider)
    assert isinstance(remote, RerankProvider)


# ---------------------------------------------------------------------------
# LocalRerankProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_returns_empty_for_empty_candidates() -> None:
    # Does not touch sentence-transformers — pure guard clause.
    provider = LocalRerankProvider(model_name="fake")
    out = await provider.rerank("query", [], top_k=5)
    assert out == []


@pytest.mark.asyncio
async def test_local_sorts_by_score_desc_and_truncates() -> None:
    pytest.importorskip("sentence_transformers")

    # Stub out `_load` so we don't actually fetch a model.
    class FakeModel:
        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            # Encode a deterministic score per content string.
            table = {"alpha": 0.1, "beta": 0.9, "gamma": 0.5}
            return [table[content] for _, content in pairs]

    provider = LocalRerankProvider(model_name="fake")
    provider._model = FakeModel()  # bypass lazy load

    candidates = [(10, "alpha"), (20, "beta"), (30, "gamma")]
    out = await provider.rerank("q", candidates, top_k=2)

    assert len(out) == 2
    assert [h.chunk_id for h in out] == [20, 30]  # beta (0.9) > gamma (0.5)
    assert out[0].score == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# RemoteRerankProvider
# ---------------------------------------------------------------------------


def _mock_client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


@pytest.mark.asyncio
async def test_remote_posts_cohere_style_body_and_parses_results() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        payload = {
            "results": [
                {"index": 1, "relevance_score": 0.91},
                {"index": 0, "relevance_score": 0.42},
            ],
        }
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    client = _mock_client(transport)
    try:
        provider = RemoteRerankProvider(
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model="rerank-multilingual-v3.0",
            client=client,
        )
        candidates = [(100, "doc A"), (200, "doc B")]
        hits = await provider.rerank("my query", candidates, top_k=2)

        # Result plumbing: highest-score first, mapped back to chunk ids.
        assert hits == [
            RerankHit(chunk_id=200, score=0.91),
            RerankHit(chunk_id=100, score=0.42),
        ]
        # Wire format: URL, auth header, body shape.
        assert captured["url"] == "https://api.example.com/v1/rerank"
        assert captured["auth"] == "Bearer sk-test"
        body = captured["body"]
        assert isinstance(body, dict)
        assert body["model"] == "rerank-multilingual-v3.0"
        assert body["query"] == "my query"
        assert body["documents"] == ["doc A", "doc B"]
        assert body["top_n"] == 2
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_remote_returns_single_hit_from_minimal_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": 0.77}]},
        )

    client = _mock_client(httpx.MockTransport(handler))
    try:
        provider = RemoteRerankProvider(
            base_url="http://local.test", api_key="k", model="m", client=client
        )
        hits = await provider.rerank("q", [(42, "only doc")], top_k=3)
        assert hits == [RerankHit(chunk_id=42, score=0.77)]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_remote_returns_empty_for_empty_candidates() -> None:
    # Must short-circuit without hitting the transport.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not be called")

    client = _mock_client(httpx.MockTransport(handler))
    try:
        provider = RemoteRerankProvider(
            base_url="http://x", api_key="k", model="m", client=client
        )
        assert await provider.rerank("q", [], top_k=5) == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_remote_drops_malformed_result_rows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "results": [
                {"index": 0, "relevance_score": 0.5},
                {"relevance_score": 0.9},  # missing `index` → dropped
                {"index": 99, "relevance_score": 0.8},  # out of range → dropped
            ],
        }
        return httpx.Response(200, json=payload)

    client = _mock_client(httpx.MockTransport(handler))
    try:
        provider = RemoteRerankProvider(
            base_url="http://x", api_key="k", model="m", client=client
        )
        hits = await provider.rerank("q", [(7, "only")], top_k=5)
        assert hits == [RerankHit(chunk_id=7, score=0.5)]
    finally:
        await client.aclose()
