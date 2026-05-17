"""Public facade — Python port of ``corlinman_vector::query::VectorStore``.

Combines :class:`SqliteStore` and :class:`UsearchIndex` into a single
hybrid-search entry point.

- :meth:`VectorStore.query` delegates to :meth:`HybridSearcher.search`
  (HNSW + BM25 + RRF fusion).
- :meth:`VectorStore.query_dense` and :meth:`VectorStore.query_sparse`
  expose single-path fallbacks for A/B comparisons and graceful
  degradation when one recall path is unavailable.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from corlinman_embedding.vector.bm25_store import SqliteStore
from corlinman_embedding.vector.fusion import (
    HybridParams,
    HybridSearcher,
    RagHit,
)
from corlinman_embedding.vector.hnsw_store import UsearchIndex

__all__ = ["VectorStore"]


class VectorStore:
    """A SQLite chunk store + one loaded usearch index, wired through
    :class:`HybridSearcher`.
    """

    __slots__ = ("_sqlite", "_index", "_hybrid")

    def __init__(
        self,
        sqlite: SqliteStore,
        index: UsearchIndex,
        params: HybridParams | None = None,
    ) -> None:
        self._sqlite = sqlite
        self._index = index
        self._hybrid = HybridSearcher(sqlite, index, params)

    # ------------------------------------------------------------------

    @classmethod
    async def open(
        cls,
        sqlite_path: str | os.PathLike[str],
        usearch_path: str | os.PathLike[str],
        params: HybridParams | None = None,
    ) -> "VectorStore":
        """Open a SQLite file + its associated ``.usearch`` file."""

        sqlite = await SqliteStore.open(Path(sqlite_path))
        index = UsearchIndex.open(Path(usearch_path))
        return cls(sqlite, index, params)

    @classmethod
    def from_parts(
        cls,
        sqlite: SqliteStore,
        index: UsearchIndex,
        params: HybridParams | None = None,
    ) -> "VectorStore":
        """Construct from already-opened components (primarily for tests)."""

        return cls(sqlite, index, params)

    @property
    def sqlite(self) -> SqliteStore:
        return self._sqlite

    @property
    def index(self) -> UsearchIndex:
        return self._index

    @property
    def hybrid(self) -> HybridSearcher:
        return self._hybrid

    # ------------------------------------------------------------------

    async def query(
        self,
        query_text: str,
        query_vector: Sequence[float],
        top_k: int,
    ) -> list[RagHit]:
        """Hybrid query — HNSW + BM25 + RRF fusion, ``top_k`` final results."""

        # Build an override that only changes top_k (mirrors Rust's behaviour).
        base = self._hybrid.params
        overrides = HybridParams(
            top_k=top_k,
            overfetch_multiplier=base.overfetch_multiplier,
            bm25_weight=base.bm25_weight,
            hnsw_weight=base.hnsw_weight,
            rrf_k=base.rrf_k,
            tag_filter=base.tag_filter,
            namespaces=base.namespaces,
            rerank_enabled=base.rerank_enabled,
            tag_subtree=base.tag_subtree,
            boost=base.boost,
        )
        return await self._hybrid.search(query_text, query_vector, overrides)

    async def query_dense(
        self,
        query_vector: Sequence[float],
        top_k: int,
    ) -> list[RagHit]:
        """Dense-only fallback (HNSW)."""

        return await self._hybrid.search_dense_only(query_vector, top_k)

    async def query_sparse(
        self,
        query_text: str,
        top_k: int,
    ) -> list[RagHit]:
        """Sparse-only fallback (BM25)."""

        return await self._hybrid.search_sparse_only(query_text, top_k)

    async def close(self) -> None:
        """Close the underlying SQLite connection. Idempotent."""

        await self._sqlite.close()
