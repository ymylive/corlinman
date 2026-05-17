"""Hybrid HNSW + BM25 retrieval with RRF fusion — Python port of `hybrid.rs`.

Wires :class:`SqliteStore` (BM25) + :class:`UsearchIndex` (HNSW) into a
single async pipeline:

1. Resolve any tag / subtree / namespace filters into a chunk-id whitelist.
2. Run dense (HNSW) and sparse (BM25) recall, each fetched at
   ``top_k * overfetch_multiplier`` candidates.
3. Fuse with weighted reciprocal-rank-fusion (:func:`rrf_fuse`).
4. Optionally multiply each candidate's RRF score by a
   :class:`CandidateBoost` factor (EPA reweight).
5. Hydrate ids into :class:`RagHit` rows.
6. Optionally hand the fused list to a :class:`Reranker`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Sequence

from corlinman_embedding.vector.bm25_store import SqliteStore
from corlinman_embedding.vector.hnsw_store import UsearchIndex
from corlinman_embedding.vector.rerank import NoopReranker, Reranker
from corlinman_embedding.vector.rrf import HitSource, rrf_fuse

__all__ = [
    "TagFilter",
    "HybridParams",
    "RagHit",
    "CandidateBoost",
    "EpaBoost",
    "HybridSearcher",
    "HitSource",
    "dynamic_boost",
]


# ---------------------------------------------------------------------------
# TagFilter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TagFilter:
    """Tag-filter predicate pushed down to both recall paths.

    All conditions conjoined:

    - ``required``: chunk must carry *every* tag in the list.
    - ``any_of``: chunk must carry *at least one* tag (ignored when empty).
    - ``excluded``: chunk must carry *none* of the listed tags.
    """

    required: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    any_of: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (self.required or self.excluded or self.any_of)


# ---------------------------------------------------------------------------
# CandidateBoost
# ---------------------------------------------------------------------------


class CandidateBoost(ABC):
    """Post-RRF candidate reweighter.

    Returns a multiplicative factor in ``(0, inf)`` for a given ``chunk_id``;
    :class:`HybridSearcher` multiplies the chunk's RRF score by this factor
    before the final sort + truncate. ``1.0`` means "no change" and is what
    implementations must return for chunks they don't recognise.

    Two entry points:

    - :meth:`prepare(ids)` runs once per query before RRF, async.
    - :meth:`boost(chunk_id)` runs inside fusion, sync. Must return ``1.0``
      for unknown ids.
    """

    async def prepare(self, chunk_ids: Sequence[int]) -> None:  # noqa: B027 - default no-op
        """Optional async prefetch hook."""

        return None

    @abstractmethod
    def boost(self, chunk_id: int) -> float: ...


def dynamic_boost(
    logic_depth: float,
    resonance_boost: float,
    entropy_penalty: float,
    base_tag_boost: float,
    boost_range: tuple[float, float] = (0.5, 2.5),
) -> float:
    """Pure-Python port of the ``dynamic_boost`` formula (mirrors `dynamic_boost_rust`).

    Inputs are clamped to ``[0, 1]`` before being fed into the formula;
    the final value is clamped to ``boost_range``.
    """

    ld = min(max(logic_depth, 0.0), 1.0)
    rb = min(max(resonance_boost, 0.0), 1.0)
    ep = min(max(entropy_penalty, 0.0), 1.0)
    denom = 1.0 + ep * 0.5  # ep ∈ [0,1] ⇒ denom ∈ [1, 1.5]
    factor = ld * (1.0 + rb) / denom
    value = base_tag_boost * factor
    return min(max(value, boost_range[0]), boost_range[1])


class EpaBoost(CandidateBoost):
    """:class:`CandidateBoost` sourcing its signal from the ``chunk_epa`` cache.

    Lookups happen inside :meth:`prepare` (async, once per query) and are
    stashed in an internal cache so the sync :meth:`boost` hot path can
    read without touching SQLite. Chunks without an EPA row produce a
    cached ``1.0`` (pass-through).
    """

    def __init__(
        self,
        store: SqliteStore,
        base_tag_boost: float = 1.0,
        boost_range: tuple[float, float] = (0.5, 2.5),
    ) -> None:
        self._store = store
        self._base_tag_boost = base_tag_boost
        self._boost_range = boost_range
        self._cache: dict[int, float] = {}

    async def prepare(self, chunk_ids: Sequence[int]) -> None:
        self._cache = {}
        out: dict[int, float] = {}
        for cid in chunk_ids:
            row = await self._store.get_chunk_epa(int(cid))
            if row is None:
                factor = 1.0
            else:
                factor = dynamic_boost(
                    row.logic_depth,
                    0.0,
                    0.0,
                    self._base_tag_boost,
                    self._boost_range,
                )
            out[int(cid)] = factor
        self._cache = out

    def boost(self, chunk_id: int) -> float:
        return self._cache.get(int(chunk_id), 1.0)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"EpaBoost(base={self._base_tag_boost}, range={self._boost_range})"


# ---------------------------------------------------------------------------
# HybridParams
# ---------------------------------------------------------------------------


@dataclass
class HybridParams:
    """Reciprocal-rank-fusion tuning knobs.

    Defaults match Rust's ``HybridParams::new``: ``top_k=10``, equal
    weights, ``rrf_k=60``, namespaces unset (→ ``"general"``).
    """

    top_k: int = 10
    overfetch_multiplier: int = 3
    bm25_weight: float = 1.0
    hnsw_weight: float = 1.0
    rrf_k: float = 60.0
    tag_filter: TagFilter | None = None
    namespaces: list[str] | None = None
    rerank_enabled: bool = False
    tag_subtree: str | None = None
    boost: CandidateBoost | None = None


# ---------------------------------------------------------------------------
# RagHit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RagHit:
    """One hit emitted by the hybrid searcher."""

    chunk_id: int
    file_id: int
    content: str
    score: float
    source: HitSource
    path: str


# ---------------------------------------------------------------------------
# HybridSearcher
# ---------------------------------------------------------------------------


class HybridSearcher:
    """Owns the two storage backends + default fusion parameters."""

    __slots__ = ("_sqlite", "_usearch", "_params", "_reranker")

    def __init__(
        self,
        sqlite: SqliteStore,
        usearch: UsearchIndex,
        params: HybridParams | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self._sqlite = sqlite
        self._usearch = usearch
        self._params = params or HybridParams()
        self._reranker: Reranker = reranker or NoopReranker()

    @property
    def params(self) -> HybridParams:
        return self._params

    @property
    def reranker(self) -> Reranker:
        return self._reranker

    def with_reranker(self, reranker: Reranker) -> "HybridSearcher":
        """Replace the reranker. Returns ``self`` for chaining."""

        self._reranker = reranker
        return self

    # ------------------------------------------------------------------

    async def search(
        self,
        query_text: str,
        query_vector: Sequence[float],
        override_params: HybridParams | None = None,
    ) -> list[RagHit]:
        """Hybrid search: HNSW + BM25 + RRF fusion."""

        p = override_params or self._params
        if p.top_k <= 0:
            return []
        fetch = p.top_k * max(p.overfetch_multiplier, 1)

        # --- Tag filter pushdown -------------------------------------
        base_tag_ids: list[int] | None
        if p.tag_filter is not None and not p.tag_filter.is_empty():
            base_tag_ids = await self._sqlite.filter_chunk_ids_by_tags(
                required=p.tag_filter.required,
                any_of=p.tag_filter.any_of,
                excluded=p.tag_filter.excluded,
            )
        else:
            base_tag_ids = None

        # --- Subtree filter -----------------------------------------
        tag_ids: list[int] | None
        if base_tag_ids is None and p.tag_subtree is None:
            tag_ids = None
        elif p.tag_subtree is None:
            tag_ids = base_tag_ids
        elif base_tag_ids is None:
            tag_ids = await self._sqlite.filter_chunk_ids_by_tag_subtree(p.tag_subtree)
        else:
            sub = await self._sqlite.filter_chunk_ids_by_tag_subtree(p.tag_subtree)
            sub_set = set(sub)
            tag_ids = [i for i in base_tag_ids if i in sub_set]

        # --- Namespace filter (S9 T1). Default = ["general"] --------
        effective_namespaces = (
            list(p.namespaces) if p.namespaces else ["general"]
        )
        ns_ids = await self._sqlite.filter_chunk_ids_by_namespace(effective_namespaces)

        # Combine namespace + tag filter
        if tag_ids is None:
            allowed_ids: list[int] | None = ns_ids
        else:
            ns_set = set(ns_ids)
            allowed_ids = [i for i in tag_ids if i in ns_set]
        allowed_set = set(allowed_ids) if allowed_ids is not None else None

        if allowed_set is not None and not allowed_set:
            return []

        # --- Dense (HNSW) recall ------------------------------------
        if self._usearch.size == 0 or not query_vector:
            dense_hits: list[tuple[int, float]] = []
        else:
            # Over-fetch when allowed_set is active (usearch can't predicate).
            hnsw_k = max(fetch * 4, fetch) if allowed_set is not None else fetch
            raw = await self._usearch.asearch(query_vector, hnsw_k)
            dense_hits = [(int(k), 1.0 - float(d)) for k, d in raw]
            if allowed_set is not None:
                dense_hits = [t for t in dense_hits if t[0] in allowed_set][:fetch]

        # --- Sparse (BM25) recall ------------------------------------
        sparse_hits = await self._sqlite.search_bm25_with_filter(
            query_text, fetch, allowed_ids
        )

        # --- Fusion --------------------------------------------------
        fused = rrf_fuse(
            dense_hits,
            sparse_hits,
            hnsw_weight=p.hnsw_weight,
            bm25_weight=p.bm25_weight,
            rrf_k=p.rrf_k,
        )

        # --- Candidate boost (post-RRF reweight) --------------------
        if p.boost is not None:
            ids = [t[0] for t in fused]
            await p.boost.prepare(ids)
            reweighted: list[tuple[int, float, HitSource]] = []
            for cid, score, src in fused:
                factor = p.boost.boost(cid)
                if factor != factor or factor == float("inf") or factor <= 0:
                    # NaN / non-finite / non-positive ⇒ pass through
                    factor = 1.0
                reweighted.append((cid, score * factor, src))
            # Re-sort after reweight; tie-break by id.
            reweighted.sort(key=lambda t: (-t[1], t[0]))
            fused = reweighted

        if p.rerank_enabled:
            candidates = fused
        else:
            candidates = fused[: p.top_k]

        if not candidates:
            return []

        hits = await self._hydrate(candidates)

        if p.rerank_enabled:
            return await self._reranker.rerank(query_text, hits, p.top_k)
        return hits

    async def search_dense_only(
        self,
        query_vector: Sequence[float],
        top_k: int,
    ) -> list[RagHit]:
        """HNSW-only fallback. Score is cosine similarity (``1 - distance``)."""

        if top_k <= 0 or not query_vector or self._usearch.size == 0:
            return []
        raw = await self._usearch.asearch(query_vector, top_k)
        scored = [(int(k), 1.0 - float(d), HitSource.DENSE) for k, d in raw]
        return await self._hydrate(scored)

    async def search_sparse_only(
        self,
        query_text: str,
        top_k: int,
    ) -> list[RagHit]:
        """BM25-only fallback. Score is the negated-bm25 value."""

        if top_k <= 0 or not query_text.strip():
            return []
        raw = await self._sqlite.search_bm25(query_text, top_k)
        scored = [(int(cid), float(s), HitSource.SPARSE) for cid, s in raw]
        return await self._hydrate(scored)

    # ------------------------------------------------------------------

    async def _hydrate(
        self,
        scored: Sequence[tuple[int, float, HitSource]],
    ) -> list[RagHit]:
        ids = [t[0] for t in scored]
        chunks = await self._sqlite.query_chunks_by_ids(ids)
        if not chunks:
            return []
        files = await self._sqlite.list_files()
        path_by_file = {f.id: f.path for f in files}
        chunk_by_id = {c.id: c for c in chunks}
        out: list[RagHit] = []
        for cid, score, src in scored:
            c = chunk_by_id.get(int(cid))
            if c is None:
                continue
            out.append(
                RagHit(
                    chunk_id=c.id,
                    file_id=c.file_id,
                    content=c.content,
                    score=float(score),
                    source=src,
                    path=path_by_file.get(c.file_id, ""),
                )
            )
        return out
