"""Reciprocal-Rank-Fusion — Python port of ``corlinman_vector::hybrid::rrf_fuse``.

For each query we run two recall paths in parallel:

1. **Dense (HNSW)** via :meth:`UsearchIndex.search`.
2. **Sparse (BM25)** via :meth:`SqliteStore.search_bm25`.

Each ranked list is fused with::

    rrf_score(doc) = Σ_r  weight_r / (rrf_k + rank_r(doc))

over rankers ``r ∈ {dense, sparse}``. Documents missing from one path
contribute 0 from that path — no implicit last-rank penalty. The final
list is sorted by fused score (descending) and stable-tie-broken by
ascending chunk id (matches Rust).
"""

from __future__ import annotations

from enum import Enum
from typing import Sequence

__all__ = ["HitSource", "rrf_fuse"]


class HitSource(str, Enum):
    """Which recall path(s) surfaced a given hit."""

    DENSE = "dense"
    SPARSE = "sparse"
    BOTH = "both"


def rrf_fuse(
    dense: Sequence[tuple[int, float]],
    sparse: Sequence[tuple[int, float]],
    *,
    hnsw_weight: float = 1.0,
    bm25_weight: float = 1.0,
    rrf_k: float = 60.0,
) -> list[tuple[int, float, HitSource]]:
    """Fuse two ranked lists with weighted reciprocal-rank-fusion.

    ``dense`` and ``sparse`` are best-first; their per-item float scores are
    ignored — RRF only needs the rank. Returns ``(chunk_id, rrf_score,
    source)`` sorted by descending RRF score, tie-broken by ascending id.
    """

    k = max(rrf_k, 1.0)  # clamp to avoid div-by-zero if caller passes 0.
    # value = (score, in_dense, in_sparse)
    scores: dict[int, list[float | bool]] = {}

    for rank, (cid, _raw) in enumerate(dense):
        contrib = hnsw_weight / (k + (rank + 1.0))
        entry = scores.setdefault(cid, [0.0, False, False])
        entry[0] = float(entry[0]) + contrib
        entry[1] = True
    for rank, (cid, _raw) in enumerate(sparse):
        contrib = bm25_weight / (k + (rank + 1.0))
        entry = scores.setdefault(cid, [0.0, False, False])
        entry[0] = float(entry[0]) + contrib
        entry[2] = True

    out: list[tuple[int, float, HitSource]] = []
    for cid, (score, in_dense, in_sparse) in scores.items():
        if in_dense and in_sparse:
            src = HitSource.BOTH
        elif in_dense:
            src = HitSource.DENSE
        elif in_sparse:
            src = HitSource.SPARSE
        else:  # pragma: no cover - unreachable; entry came from at least one ranker
            raise AssertionError("score entry must come from at least one ranker")
        out.append((int(cid), float(score), src))

    # descending score, stable tie-break by ascending id
    out.sort(key=lambda t: (-t[1], t[0]))
    return out
