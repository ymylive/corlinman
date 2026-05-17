"""corlinman_embedding.vector — RAG persistence + hybrid retrieval.

Python port of the Rust crate ``corlinman-vector``. Native corlinman RAG is
a three-part pipeline:

1. **HNSW dense recall** via :class:`UsearchIndex` (usearch 2.x, cosine).
2. **BM25 sparse recall** via :meth:`SqliteStore.search_bm25`
   (SQLite FTS5 ``bm25()`` over ``chunks.content``).
3. **RRF fusion** via :class:`HybridSearcher`
   (reciprocal-rank-fusion with per-ranker weights).

Cross-encoder rerank ships as a pluggable :class:`Reranker` ABC; the
default :class:`NoopReranker` is wired into :class:`HybridSearcher`.

Module layout:

- :mod:`corlinman_embedding.vector.bm25_store` — aiosqlite + FTS5 + tag tree
  + EPA cache.
- :mod:`corlinman_embedding.vector.hnsw_store` — async wrapper over
  ``usearch-python``.
- :mod:`corlinman_embedding.vector.rrf` — pure-Python RRF fusion + HitSource enum.
- :mod:`corlinman_embedding.vector.fusion` — :class:`HybridSearcher`,
  :class:`HybridParams`, :class:`TagFilter`, :class:`RagHit`,
  :class:`CandidateBoost`, :class:`EpaBoost`.
- :mod:`corlinman_embedding.vector.store` — :class:`VectorStore` facade.
- :mod:`corlinman_embedding.vector.decay` — pure decay arithmetic.
- :mod:`corlinman_embedding.vector.rerank` — :class:`Reranker` ABC.
- :mod:`corlinman_embedding.vector.header` — read-only ``.usearch`` header probe.
"""

from __future__ import annotations

from corlinman_embedding.vector.bm25_store import (
    SCHEMA_SQL,
    SCHEMA_VERSION,
    ChunkEpaRow,
    ChunkRow,
    FileRow,
    SqliteStore,
    TagNodeRow,
    blob_to_f32_vec,
    f32_slice_to_blob,
)
from corlinman_embedding.vector.decay import (
    CONSOLIDATED_NAMESPACE,
    DecayConfig,
    apply_decay,
    boosted_score,
)
from corlinman_embedding.vector.fusion import (
    CandidateBoost,
    EpaBoost,
    HitSource,
    HybridParams,
    HybridSearcher,
    RagHit,
    TagFilter,
    dynamic_boost,
)
from corlinman_embedding.vector.header import (
    UsearchHeader,
    probe_and_convert_if_needed,
    probe_usearch_header,
)
from corlinman_embedding.vector.hnsw_store import DEFAULT_CAPACITY, UsearchIndex
from corlinman_embedding.vector.rerank import NoopReranker, Reranker
from corlinman_embedding.vector.rrf import rrf_fuse
from corlinman_embedding.vector.store import VectorStore

__all__: list[str] = [
    # schema + helpers
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "f32_slice_to_blob",
    "blob_to_f32_vec",
    # decay
    "CONSOLIDATED_NAMESPACE",
    "DecayConfig",
    "apply_decay",
    "boosted_score",
    # header
    "UsearchHeader",
    "probe_usearch_header",
    "probe_and_convert_if_needed",
    # hnsw
    "DEFAULT_CAPACITY",
    "UsearchIndex",
    # bm25 store
    "ChunkEpaRow",
    "ChunkRow",
    "FileRow",
    "SqliteStore",
    "TagNodeRow",
    # fusion
    "CandidateBoost",
    "EpaBoost",
    "HitSource",
    "HybridParams",
    "HybridSearcher",
    "RagHit",
    "TagFilter",
    "dynamic_boost",
    "rrf_fuse",
    # rerank
    "NoopReranker",
    "Reranker",
    # facade
    "VectorStore",
]
