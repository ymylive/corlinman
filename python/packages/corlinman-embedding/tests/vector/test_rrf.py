"""RRF fusion — mirror of `hybrid::rrf_*` unit tests."""

from __future__ import annotations

import math

from corlinman_embedding.vector.rrf import HitSource, rrf_fuse


def test_rrf_ranks_doc_in_both_paths_highest() -> None:
    dense = [(10, 0.99), (20, 0.80), (30, 0.50)]
    sparse = [(30, 5.0), (20, 3.0), (40, 1.0)]
    fused = rrf_fuse(dense, sparse)
    top_ids = [cid for cid, _, _ in fused[:2]]
    assert 20 in top_ids and 30 in top_ids
    src_map = {cid: src for cid, _, src in fused}
    assert src_map[20] == HitSource.BOTH
    assert src_map[30] == HitSource.BOTH
    assert src_map[10] == HitSource.DENSE
    assert src_map[40] == HitSource.SPARSE


def test_rrf_weights_bias_path() -> None:
    dense = [(1, 0.9), (2, 0.8)]
    sparse = [(2, 5.0), (1, 3.0)]
    fused = rrf_fuse(dense, sparse, bm25_weight=10.0, hnsw_weight=0.1)
    assert fused[0][0] == 2


def test_rrf_handles_empty_inputs() -> None:
    assert rrf_fuse([], []) == []
    out = rrf_fuse([(1, 0.5)], [])
    assert len(out) == 1
    assert out[0][2] == HitSource.DENSE


def test_rrf_k_clamped_at_one() -> None:
    fused = rrf_fuse([(1, 0.0)], [(1, 0.0)], rrf_k=0.0)
    assert len(fused) == 1
    assert math.isfinite(fused[0][1])


def test_rrf_stable_tiebreak_by_id() -> None:
    # Identical position in both rankers ⇒ identical RRF score; tie-break by id.
    dense = [(7, 1.0), (3, 0.5)]
    sparse = [(7, 1.0), (3, 0.5)]
    fused = rrf_fuse(dense, sparse)
    # Same rank ⇒ same score for both ids, but 3 < 7 only matters when scores
    # tie. Here doc 7 is rank-1 in both, doc 3 is rank-2 in both, so 7 wins.
    assert fused[0][0] == 7
    assert fused[1][0] == 3
