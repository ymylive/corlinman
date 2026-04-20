"""Cross-encoder rerank clients (Sprint 3 T6).

Presents a single :class:`RerankProvider` protocol to callers (the gRPC
``Rerank`` RPC handler on the embedding service, once that lands) with
two concrete implementations:

- :class:`LocalRerankProvider` — wraps a ``sentence-transformers``
  ``CrossEncoder`` (e.g. ``BAAI/bge-reranker-v2-m3``). Model load is
  deferred to the first ``rerank`` call so importing this module does
  not pull Torch unless the local path is actually selected. The
  ``sentence-transformers`` dependency lives in the ``[local]`` extra.

- :class:`RemoteRerankProvider` — POSTs to a cohere / siliconflow /
  OpenAI-compat rerank endpoint via ``httpx``. No heavyweight deps.

Both return :class:`RerankHit` records ordered best-first with length
``≤ top_k``.

The Rust side (``corlinman_vector::rerank::GrpcReranker``) is a stub
until the ``Rerank`` RPC is declared in ``proto/embedding.proto``; this
Python module is standalone-callable today and will be hooked in once
the proto lands.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx
import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RerankHit:
    """One reranked candidate. ``score`` is the cross-encoder output —
    higher is better. Ordering within a result list is best-first."""

    chunk_id: int
    score: float


@runtime_checkable
class RerankProvider(Protocol):
    """Common interface for every rerank backend."""

    async def rerank(
        self,
        query: str,
        candidates: Sequence[tuple[int, str]],
        top_k: int,
    ) -> list[RerankHit]:
        """Score every ``(query, candidate_text)`` pair and return the top
        ``top_k`` sorted best-first.

        ``candidates`` is a sequence of ``(chunk_id, content)`` tuples.
        The provider must not mutate it. Implementations may short-circuit
        when ``candidates`` is empty.
        """
        ...


# ---------------------------------------------------------------------------
# Local (sentence-transformers CrossEncoder)
# ---------------------------------------------------------------------------


class LocalRerankProvider:
    """Local sentence-transformers cross-encoder.

    The model is loaded lazily (on first ``rerank`` call) so this class
    can be instantiated — and imported — on hosts without the ``[local]``
    extra. Attempting to actually call ``rerank`` without
    ``sentence-transformers`` installed raises ``ImportError`` with a
    pointer to the extra.
    """

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3") -> None:
        self.model_name = model_name
        self._model: object | None = None  # populated on first use

    def _load(self) -> object:
        """Import + instantiate the CrossEncoder. Cached in ``self._model``."""
        if self._model is not None:
            return self._model
        try:
            # Deferred import: keeps the slim image (no torch) importable.
            from sentence_transformers import CrossEncoder  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover — exercised via importorskip
            raise ImportError(
                "LocalRerankProvider requires the `sentence-transformers` "
                "package. Install via `pip install corlinman-embedding[local]`."
            ) from exc
        logger.info("loading cross-encoder model", model=self.model_name)
        self._model = CrossEncoder(self.model_name)
        return self._model

    async def rerank(
        self,
        query: str,
        candidates: Sequence[tuple[int, str]],
        top_k: int,
    ) -> list[RerankHit]:
        if not candidates or top_k <= 0:
            return []
        model = self._load()
        pairs = [(query, content) for _, content in candidates]
        # CrossEncoder.predict is sync / CPU-bound. We're already on a
        # gRPC worker thread by the time this runs so a plain call is
        # fine; no need for run_in_executor shenanigans until profiling
        # says otherwise.
        scores = model.predict(pairs)  # type: ignore[attr-defined]
        hits = [
            RerankHit(chunk_id=chunk_id, score=float(score))
            for (chunk_id, _), score in zip(candidates, scores, strict=True)
        ]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]


# ---------------------------------------------------------------------------
# Remote (cohere / siliconflow / OpenAI-compat rerank endpoint)
# ---------------------------------------------------------------------------


class RemoteRerankProvider:
    """HTTP rerank client.

    Targets the cohere-style rerank API (``POST {base_url}/rerank`` with
    ``{"query", "documents", "model", "top_n"}``). SiliconFlow's
    rerank endpoint uses the same shape and is interchangeable.

    The response is expected to carry ``results: [{index, relevance_score}]``
    where ``index`` is a 0-based offset into ``documents``. We translate
    that back to the caller's ``chunk_id``.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._timeout = timeout
        self._owned_client = client is None
        # Callers (tests) can inject a client backed by ``httpx.MockTransport``;
        # otherwise we create one lazily per call so the default path stays
        # thread-safe without a global.
        self._client: httpx.AsyncClient | None = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owned_client = True
        return self._client

    async def aclose(self) -> None:
        """Close the owned HTTP client, if any. Idempotent."""
        if self._client is not None and self._owned_client:
            await self._client.aclose()
            self._client = None

    async def rerank(
        self,
        query: str,
        candidates: Sequence[tuple[int, str]],
        top_k: int,
    ) -> list[RerankHit]:
        if not candidates or top_k <= 0:
            return []
        chunk_ids = [cid for cid, _ in candidates]
        documents = [content for _, content in candidates]
        payload = {
            "model": self.model,
            "query": query,
            "documents": documents,
            "top_n": top_k,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/rerank"
        client = await self._get_client()
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        results = body.get("results", [])
        hits: list[RerankHit] = []
        for item in results:
            idx = item.get("index")
            score = item.get("relevance_score", item.get("score"))
            if idx is None or score is None:
                continue
            if not 0 <= idx < len(chunk_ids):
                continue
            hits.append(RerankHit(chunk_id=chunk_ids[idx], score=float(score)))
        # Providers usually return results best-first already; sort defensively.
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]


__all__ = [
    "LocalRerankProvider",
    "RemoteRerankProvider",
    "RerankHit",
    "RerankProvider",
]
