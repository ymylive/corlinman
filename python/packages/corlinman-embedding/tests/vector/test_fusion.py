"""HybridSearcher + VectorStore — mirror of the `hybrid.rs` + `query.rs` tests."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from corlinman_embedding.vector.bm25_store import SqliteStore
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
from corlinman_embedding.vector.hnsw_store import UsearchIndex
from corlinman_embedding.vector.rerank import Reranker
from corlinman_embedding.vector.store import VectorStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _tiny_store(tmp_path: Path) -> tuple[VectorStore, SqliteStore, UsearchIndex]:
    sqlite = await SqliteStore.open(tmp_path / "kb.sqlite")
    file_id = await sqlite.insert_file("公共/fixture.md", "公共", "abc", 1, 1)
    corpus = [
        ("apple banana cherry", [1.0, 0.0, 0.0, 0.0]),
        ("dog elephant fox", [0.0, 1.0, 0.0, 0.0]),
        ("grape honey iris", [0.0, 0.0, 1.0, 0.0]),
    ]
    index = UsearchIndex.create_with_capacity(4, 16)
    for i, (text, vec) in enumerate(corpus):
        cid = await sqlite.insert_chunk(file_id, i, text, vec, "general")
        index.add(cid, vec)
    return VectorStore.from_parts(sqlite, index), sqlite, index


async def _tagged_store(tmp_path: Path) -> tuple[HybridSearcher, SqliteStore]:
    sqlite = await SqliteStore.open(tmp_path / "kb.sqlite")
    file_id = await sqlite.insert_file("notes/t.md", "notes", "h", 0, 0)
    corpus = [
        ("apple banana cherry", [1.0, 0.0, 0.0, 0.0]),
        ("banana dog elephant", [0.9, 0.1, 0.0, 0.0]),
        ("grape honey iris", [0.0, 0.0, 1.0, 0.0]),
    ]
    ids: list[int] = []
    index = UsearchIndex.create_with_capacity(4, 16)
    for i, (text, vec) in enumerate(corpus):
        cid = await sqlite.insert_chunk(file_id, i, text, vec, "general")
        index.add(cid, vec)
        ids.append(cid)
    # ids[0] → rust+backend; ids[1] → rust+frontend; ids[2] → untagged.
    await sqlite.insert_tag(ids[0], "rust")
    await sqlite.insert_tag(ids[0], "backend")
    await sqlite.insert_tag(ids[1], "rust")
    await sqlite.insert_tag(ids[1], "frontend")
    return HybridSearcher(sqlite, index), sqlite


async def _subtree_store(tmp_path: Path) -> HybridSearcher:
    sqlite = await SqliteStore.open(tmp_path / "kb.sqlite")
    file_id = await sqlite.insert_file("notes/st.md", "notes", "h", 0, 0)
    corpus = [
        ("alpha word", [1.0, 0.0, 0.0, 0.0], "role.protagonist.voice"),
        ("bravo word", [0.9, 0.1, 0.0, 0.0], "role.antagonist"),
        ("charlie word", [0.0, 0.0, 1.0, 0.0], "mood.calm"),
        ("delta word", [0.0, 1.0, 0.0, 0.0], ""),
    ]
    index = UsearchIndex.create_with_capacity(4, 16)
    for i, (text, vec, path) in enumerate(corpus):
        cid = await sqlite.insert_chunk(file_id, i, text, vec, "general")
        index.add(cid, vec)
        if path:
            await sqlite.attach_chunk_to_tag_path(cid, path)
    return HybridSearcher(sqlite, index)


async def _namespaced_store(tmp_path: Path) -> HybridSearcher:
    sqlite = await SqliteStore.open(tmp_path / "kb.sqlite")
    file_id = await sqlite.insert_file("ns.md", "ns", "h", 0, 0)
    rows = [
        ("apple banana cherry", [1.0, 0.0, 0.0, 0.0], "general"),
        ("banana dog", [0.9, 0.1, 0.0, 0.0], "general"),
        ("banana rain", [0.8, 0.0, 0.2, 0.0], "diary:a"),
        ("banana snow", [0.0, 1.0, 0.0, 0.0], "diary:a"),
    ]
    index = UsearchIndex.create_with_capacity(4, 16)
    for i, (text, vec, ns) in enumerate(rows):
        cid = await sqlite.insert_chunk(file_id, i, text, vec, ns)
        index.add(cid, vec)
    return HybridSearcher(sqlite, index)


# ---------------------------------------------------------------------------
# VectorStore (the public facade)
# ---------------------------------------------------------------------------


async def test_hybrid_query_surfaces_matching_chunk(tmp_path: Path) -> None:
    store, _s, _i = await _tiny_store(tmp_path)
    hits = await store.query("banana", [1.0, 0.0, 0.0, 0.0], 3)
    assert hits
    assert "banana" in hits[0].content
    assert hits[0].path == "公共/fixture.md"
    assert hits[0].source == HitSource.BOTH


async def test_query_top_k_zero_is_empty(tmp_path: Path) -> None:
    store, _s, _i = await _tiny_store(tmp_path)
    hits = await store.query("banana", [1.0, 0.0, 0.0, 0.0], 0)
    assert hits == []


async def test_query_dense_only_works(tmp_path: Path) -> None:
    store, _s, _i = await _tiny_store(tmp_path)
    hits = await store.query_dense([1.0, 0.0, 0.0, 0.0], 2)
    assert len(hits) == 2
    assert hits[0].source == HitSource.DENSE


async def test_query_sparse_only_works(tmp_path: Path) -> None:
    store, _s, _i = await _tiny_store(tmp_path)
    hits = await store.query_sparse("elephant", 2)
    assert hits
    assert "elephant" in hits[0].content
    assert hits[0].source == HitSource.SPARSE


async def test_query_empty_index_is_empty_when_text_also_empty(tmp_path: Path) -> None:
    sqlite = await SqliteStore.open(tmp_path / "kb.sqlite")
    index = UsearchIndex.create_with_capacity(4, 4)
    store = VectorStore.from_parts(sqlite, index)
    hits = await store.query("", [1.0, 0.0, 0.0, 0.0], 5)
    assert hits == []


# ---------------------------------------------------------------------------
# tag filter
# ---------------------------------------------------------------------------


def _params_with_filter(top_k: int, tf: TagFilter) -> HybridParams:
    return HybridParams(top_k=top_k, tag_filter=tf)


async def test_tag_filter_required_matches_only_those_tags(tmp_path: Path) -> None:
    searcher, _ = await _tagged_store(tmp_path)
    tf = TagFilter(required=("rust",))
    hits = await searcher.search(
        "banana", [1.0, 0.0, 0.0, 0.0], _params_with_filter(10, tf)
    )
    assert len(hits) == 2
    for h in hits:
        assert "grape" not in h.content


async def test_tag_filter_excluded_removes_matches(tmp_path: Path) -> None:
    searcher, _ = await _tagged_store(tmp_path)
    tf = TagFilter(excluded=("frontend",))
    hits = await searcher.search(
        "banana grape", [1.0, 0.0, 0.0, 0.0], _params_with_filter(10, tf)
    )
    contents = [h.content for h in hits]
    assert any("apple" in c for c in contents)
    assert not any("dog elephant" in c for c in contents)


async def test_tag_filter_any_of_ors(tmp_path: Path) -> None:
    searcher, _ = await _tagged_store(tmp_path)
    tf = TagFilter(any_of=("backend", "frontend"))
    hits = await searcher.search(
        "banana", [1.0, 0.0, 0.0, 0.0], _params_with_filter(10, tf)
    )
    assert len(hits) == 2


async def test_tag_filter_empty_equivalent_to_no_filter(tmp_path: Path) -> None:
    searcher, _ = await _tagged_store(tmp_path)
    with_empty = await searcher.search(
        "banana", [1.0, 0.0, 0.0, 0.0], _params_with_filter(10, TagFilter())
    )
    without = await searcher.search("banana", [1.0, 0.0, 0.0, 0.0], None)
    assert len(with_empty) == len(without)


async def test_tag_filter_combined_required_and_excluded(tmp_path: Path) -> None:
    searcher, _ = await _tagged_store(tmp_path)
    tf = TagFilter(required=("rust",), excluded=("frontend",))
    hits = await searcher.search(
        "banana", [1.0, 0.0, 0.0, 0.0], _params_with_filter(10, tf)
    )
    assert len(hits) == 1
    assert "apple" in hits[0].content


# ---------------------------------------------------------------------------
# subtree filter
# ---------------------------------------------------------------------------


def _params_with_subtree(top_k: int, root: str) -> HybridParams:
    return HybridParams(top_k=top_k, tag_subtree=root)


async def test_subtree_filter_matches_nested_paths(tmp_path: Path) -> None:
    searcher = await _subtree_store(tmp_path)
    hits = await searcher.search(
        "word", [1.0, 0.0, 0.0, 0.0], _params_with_subtree(10, "role")
    )
    contents = [h.content for h in hits]
    assert len(hits) == 2, f"got {contents}"
    assert any("alpha" in c for c in contents)
    assert any("bravo" in c for c in contents)
    assert not any("charlie" in c for c in contents)
    assert not any("delta" in c for c in contents)


async def test_subtree_filter_does_not_leak_across_roots(tmp_path: Path) -> None:
    searcher = await _subtree_store(tmp_path)
    hits = await searcher.search(
        "word", [1.0, 0.0, 0.0, 0.0], _params_with_subtree(10, "mood")
    )
    assert len(hits) == 1
    assert "charlie" in hits[0].content


# ---------------------------------------------------------------------------
# namespace filter
# ---------------------------------------------------------------------------


def _ns_params(namespaces: list[str] | None) -> HybridParams:
    return HybridParams(top_k=10, namespaces=namespaces)


async def test_namespace_filter_restricts_to_named_namespace(tmp_path: Path) -> None:
    searcher = await _namespaced_store(tmp_path)
    hits = await searcher.search(
        "banana", [1.0, 0.0, 0.0, 0.0], _ns_params(["diary:a"])
    )
    assert len(hits) == 2
    for h in hits:
        assert "rain" in h.content or "snow" in h.content


async def test_namespace_none_defaults_to_general_only(tmp_path: Path) -> None:
    searcher = await _namespaced_store(tmp_path)
    hits = await searcher.search("banana", [1.0, 0.0, 0.0, 0.0], _ns_params(None))
    assert len(hits) == 2
    for h in hits:
        assert "apple" in h.content or "dog" in h.content


async def test_namespace_empty_list_treated_as_none(tmp_path: Path) -> None:
    searcher = await _namespaced_store(tmp_path)
    hits = await searcher.search("banana", [1.0, 0.0, 0.0, 0.0], _ns_params([]))
    assert len(hits) == 2  # same as None → general only


async def test_namespace_multi_value_union(tmp_path: Path) -> None:
    searcher = await _namespaced_store(tmp_path)
    hits = await searcher.search(
        "banana",
        [1.0, 0.0, 0.0, 0.0],
        _ns_params(["general", "diary:a"]),
    )
    assert len(hits) == 4


# ---------------------------------------------------------------------------
# Reranker integration
# ---------------------------------------------------------------------------


class ReversingReranker(Reranker):
    """Reverses RRF order — used to detect whether the reranker fired."""

    async def rerank(self, query: str, hits: list[RagHit], top_k: int) -> list[RagHit]:
        return list(reversed(hits))[:top_k]


def _rerank_params(top_k: int, enabled: bool) -> HybridParams:
    return HybridParams(top_k=top_k, rerank_enabled=enabled)


async def test_rerank_disabled_preserves_rrf_order(tmp_path: Path) -> None:
    searcher, _ = await _tagged_store(tmp_path)
    searcher.with_reranker(ReversingReranker())
    hits = await searcher.search(
        "banana", [1.0, 0.0, 0.0, 0.0], _rerank_params(10, False)
    )
    assert hits
    assert "apple" in hits[0].content


async def test_rerank_enabled_uses_injected_reranker(tmp_path: Path) -> None:
    searcher, _ = await _tagged_store(tmp_path)
    baseline = await searcher.search(
        "banana", [1.0, 0.0, 0.0, 0.0], _rerank_params(10, False)
    )
    searcher.with_reranker(ReversingReranker())
    reranked = await searcher.search(
        "banana", [1.0, 0.0, 0.0, 0.0], _rerank_params(10, True)
    )
    assert len(baseline) == len(reranked)
    assert len(baseline) >= 2
    assert reranked[0].chunk_id == baseline[-1].chunk_id
    assert reranked[-1].chunk_id == baseline[0].chunk_id


async def test_rerank_enabled_truncates_to_top_k(tmp_path: Path) -> None:
    searcher, _ = await _tagged_store(tmp_path)
    searcher.with_reranker(ReversingReranker())
    hits = await searcher.search(
        "banana grape", [1.0, 0.0, 0.0, 0.0], _rerank_params(2, True)
    )
    assert len(hits) <= 2


# ---------------------------------------------------------------------------
# dynamic_boost + EpaBoost
# ---------------------------------------------------------------------------


def test_dynamic_boost_clamps_to_range() -> None:
    hi = dynamic_boost(1.0, 0.0, 0.0, 10.0, (0.5, 2.5))
    assert math.isclose(hi, 2.5, abs_tol=1e-6)
    lo = dynamic_boost(0.0, 0.0, 0.0, 1.0, (0.5, 2.5))
    assert math.isclose(lo, 0.5, abs_tol=1e-6)
    capped = dynamic_boost(99.0, 99.0, 0.0, 1.0, (0.5, 2.5))
    assert math.isclose(capped, 2.0, abs_tol=1e-6)


async def test_epa_boost_returns_one_for_missing_row(tmp_path: Path) -> None:
    sqlite = await SqliteStore.open(tmp_path / "kb.sqlite")
    file_id = await sqlite.insert_file("e.md", "default", "h", 0, 0)
    cid = await sqlite.insert_chunk(file_id, 0, "x", None, "general")
    booster = EpaBoost(sqlite, 1.0, (0.5, 2.5))
    await booster.prepare([cid])
    assert math.isclose(booster.boost(cid), 1.0, abs_tol=1e-6)
    assert math.isclose(booster.boost(9999), 1.0, abs_tol=1e-6)


async def test_epa_boost_uses_logic_depth(tmp_path: Path) -> None:
    sqlite = await SqliteStore.open(tmp_path / "kb.sqlite")
    file_id = await sqlite.insert_file("e.md", "default", "h", 0, 0)
    high = await sqlite.insert_chunk(file_id, 0, "h", None, "general")
    low = await sqlite.insert_chunk(file_id, 1, "l", None, "general")
    await sqlite.upsert_chunk_epa(high, [0.5, 0.1], 0.3, 0.9)
    await sqlite.upsert_chunk_epa(low, [0.2, 0.4], 0.8, 0.1)
    booster = EpaBoost(sqlite, 1.0, (0.5, 2.5))
    await booster.prepare([high, low])
    assert booster.boost(high) > booster.boost(low)
    expected = dynamic_boost(0.9, 0.0, 0.0, 1.0, (0.5, 2.5))
    assert math.isclose(booster.boost(high), expected, abs_tol=1e-6)


def _boost_params(top_k: int, boost: CandidateBoost | None) -> HybridParams:
    return HybridParams(top_k=top_k, boost=boost)


async def test_hybrid_search_without_boost_is_byte_identical_to_baseline(tmp_path: Path) -> None:
    searcher, _ = await _tagged_store(tmp_path)
    baseline = await searcher.search(
        "banana", [1.0, 0.0, 0.0, 0.0], _boost_params(10, None)
    )
    repeat = await searcher.search(
        "banana", [1.0, 0.0, 0.0, 0.0], _boost_params(10, None)
    )
    assert len(baseline) == len(repeat)
    for a, b in zip(baseline, repeat):
        assert a.chunk_id == b.chunk_id
        assert math.isclose(a.score, b.score, abs_tol=1e-9)
        assert a.source == b.source


async def test_hybrid_search_with_epa_boost_reranks_higher_logic_depth(
    tmp_path: Path,
) -> None:
    sqlite = await SqliteStore.open(tmp_path / "kb.sqlite")
    file_id = await sqlite.insert_file("r.md", "r", "h", 0, 0)

    low_id = await sqlite.insert_chunk(file_id, 0, "banana one", [1.0, 0.0, 0.0, 0.0], "general")
    high_id = await sqlite.insert_chunk(
        file_id, 1, "banana two", [0.99, 0.01, 0.0, 0.0], "general"
    )

    await sqlite.upsert_chunk_epa(low_id, [0.1, 0.2], 0.95, 0.05)
    await sqlite.upsert_chunk_epa(high_id, [0.3, 0.4], 0.05, 0.95)

    index = UsearchIndex.create_with_capacity(4, 16)
    index.add(low_id, [1.0, 0.0, 0.0, 0.0])
    index.add(high_id, [0.99, 0.01, 0.0, 0.0])
    searcher = HybridSearcher(sqlite, index)

    baseline = await searcher.search("banana", [1.0, 0.0, 0.0, 0.0], None)
    assert len(baseline) >= 2
    assert baseline[0].chunk_id == low_id

    booster = EpaBoost(sqlite, 1.0, (0.5, 2.5))
    boosted = await searcher.search(
        "banana", [1.0, 0.0, 0.0, 0.0], _boost_params(10, booster)
    )
    assert boosted[0].chunk_id == high_id


# ---------------------------------------------------------------------------
# Smoke: end-to-end VectorStore.open from disk-backed files
# ---------------------------------------------------------------------------


async def test_vector_store_open_disk_roundtrip(tmp_path: Path) -> None:
    # Build store on disk, save the usearch index, then reopen via .open().
    sqlite_path = tmp_path / "kb.sqlite"
    usearch_path = tmp_path / "idx.usearch"

    sqlite = await SqliteStore.open(sqlite_path)
    file_id = await sqlite.insert_file("公共/fixture.md", "公共", "h", 0, 0)
    chunks = [
        ("alpha bravo charlie", [1.0, 0.0, 0.0, 0.0]),
        ("delta echo foxtrot", [0.0, 1.0, 0.0, 0.0]),
    ]
    index = UsearchIndex.create_with_capacity(4, 16)
    for i, (text, vec) in enumerate(chunks):
        cid = await sqlite.insert_chunk(file_id, i, text, vec, "general")
        index.add(cid, vec)
    index.save(usearch_path)
    await sqlite.close()

    store = await VectorStore.open(sqlite_path, usearch_path)
    try:
        hits = await store.query("alpha", [1.0, 0.0, 0.0, 0.0], 2)
        assert hits
        assert hits[0].content.startswith("alpha")
        assert hits[0].path == "公共/fixture.md"
    finally:
        await store.close()
