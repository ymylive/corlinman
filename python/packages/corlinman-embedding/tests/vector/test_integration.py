"""End-to-end integration tests — mirror of Rust ``tests/integration.rs``.

Builds a deterministic fixture (seeded PRNG) of 20 mixed-language chunks
with 16-dim normalised vectors, then exercises the full VectorStore
pipeline.
"""

from __future__ import annotations

import math
from pathlib import Path

from corlinman_embedding.vector.bm25_store import SqliteStore
from corlinman_embedding.vector.fusion import HitSource, HybridParams, HybridSearcher
from corlinman_embedding.vector.hnsw_store import UsearchIndex
from corlinman_embedding.vector.store import VectorStore

NUM_CHUNKS = 20
DIM = 16
SEED = 0x1234_5678_9ABC_DEF0


class Xorshift:
    """Tiny seeded PRNG (xorshift64*) — matches the Rust integration fixture."""

    def __init__(self, seed: int) -> None:
        self.state = max(1, seed) & ((1 << 64) - 1)

    def next_u64(self) -> int:
        x = self.state
        x ^= (x << 13) & ((1 << 64) - 1)
        x ^= x >> 7
        x ^= (x << 17) & ((1 << 64) - 1)
        self.state = x
        return (x * 0x2545_F491_4F6C_DD1D) & ((1 << 64) - 1)

    def next_f32_symmetric(self) -> float:
        bits = (self.next_u64() >> 40) & 0xFFFFFF
        return (bits / float(1 << 23)) - 1.0

    def next_range(self, n: int) -> int:
        return self.next_u64() % max(1, n)


def _normalize(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v))
    if norm > 1e-12:
        return [x / norm for x in v]
    return v


EN_WORDS = [
    "corlinman", "orchard", "pipeline", "relay", "beacon", "cascade",
    "nimbus", "forge", "meadow", "quartz", "silhouette", "thicket",
    "voyager", "wistful", "zenith", "borealis",
]
ZH_WORDS = [
    "日志", "检索", "融合", "索引", "向量", "关键词", "语义",
    "沉思", "桥梁", "清晨", "黄昏", "归档", "笔记",
]


def _synth_content(rng: Xorshift, chunk_id: int) -> str:
    word_count = 4 + rng.next_range(5)
    parts = [f"chunk{chunk_id:02d}"]
    for _ in range(word_count):
        use_zh = (rng.next_u64() & 1) == 0
        pick = ZH_WORDS[rng.next_range(len(ZH_WORDS))] if use_zh else EN_WORDS[rng.next_range(len(EN_WORDS))]
        parts.append(pick)
    return " ".join(parts)


def _synth_vector(rng: Xorshift) -> list[float]:
    return _normalize([rng.next_f32_symmetric() for _ in range(DIM)])


async def _build_fixture(dir_: Path) -> tuple[Path, Path, list[tuple[str, list[float]]]]:
    sqlite_path = dir_ / "kb.sqlite"
    usearch_path = dir_ / "index.usearch"
    store = await SqliteStore.open(sqlite_path)
    file_id = await store.insert_file("公共/fixture.md", "公共", "fixturehash", 1_700_000_000, 4096)

    index = UsearchIndex.create_with_capacity(DIM, 64)
    rng = Xorshift(SEED)
    corpus: list[tuple[str, list[float]]] = []
    for i in range(NUM_CHUNKS):
        content = _synth_content(rng, i)
        vec = _synth_vector(rng)
        cid = await store.insert_chunk(file_id, i, content, vec, "general")
        index.add(cid, vec)
        corpus.append((content, vec))
    index.save(usearch_path)
    await store.close()
    return sqlite_path, usearch_path, corpus


async def test_dense_only_returns_nearest_vector_first(tmp_path: Path) -> None:
    sqlite_path, usearch_path, corpus = await _build_fixture(tmp_path)
    store = await VectorStore.open(sqlite_path, usearch_path)
    try:
        hits = await store.query_dense(corpus[0][1], 3)
        assert len(hits) == 3
        assert hits[0].content.startswith("chunk00")
        assert hits[0].source == HitSource.DENSE
        # Similarity non-increasing.
        for a, b in zip(hits, hits[1:]):
            assert a.score + 1e-6 >= b.score
    finally:
        await store.close()


async def test_sparse_only_finds_chunk_by_unique_token(tmp_path: Path) -> None:
    sqlite_path, usearch_path, _ = await _build_fixture(tmp_path)
    store = await VectorStore.open(sqlite_path, usearch_path)
    try:
        hits = await store.query_sparse("chunk05", 5)
        assert hits
        assert hits[0].content.startswith("chunk05")
        assert hits[0].source == HitSource.SPARSE
        assert hits[0].path == "公共/fixture.md"
    finally:
        await store.close()


async def test_hybrid_fusion_outranks_single_path_when_both_agree(tmp_path: Path) -> None:
    sqlite_path, usearch_path, corpus = await _build_fixture(tmp_path)
    store = await VectorStore.open(sqlite_path, usearch_path)
    try:
        marker = corpus[0][0].split()[0]  # "chunk00"
        hits = await store.query(marker, corpus[0][1], 5)
        assert hits
        assert hits[0].content.startswith("chunk00")
        assert hits[0].source == HitSource.BOTH

        dense_only = await store.query_dense(corpus[0][1], 5)
        dense_top = next((h for h in dense_only if h.content.startswith("chunk00")), None)
        assert dense_top is not None
        assert hits[0].chunk_id == dense_top.chunk_id
    finally:
        await store.close()


async def test_query_top_k_zero_returns_empty(tmp_path: Path) -> None:
    sqlite_path, usearch_path, corpus = await _build_fixture(tmp_path)
    store = await VectorStore.open(sqlite_path, usearch_path)
    try:
        assert await store.query("chunk00", corpus[0][1], 0) == []
        assert await store.query_dense(corpus[0][1], 0) == []
        assert await store.query_sparse("chunk00", 0) == []
    finally:
        await store.close()


async def test_hybrid_respects_final_top_k_cap(tmp_path: Path) -> None:
    sqlite_path, usearch_path, corpus = await _build_fixture(tmp_path)
    store = await VectorStore.open(sqlite_path, usearch_path)
    try:
        hits = await store.query("日志 检索 融合", corpus[0][1], 3)
        assert len(hits) <= 3
        for a, b in zip(hits, hits[1:]):
            assert a.score + 1e-6 >= b.score
    finally:
        await store.close()


async def test_overfetch_multiplier_controls_candidate_pool(tmp_path: Path) -> None:
    sqlite_path, usearch_path, corpus = await _build_fixture(tmp_path)
    sqlite = await SqliteStore.open(sqlite_path)
    index = UsearchIndex.open(usearch_path)
    try:
        token = "融合 索引 向量"
        narrow = HybridSearcher(sqlite, index, HybridParams(top_k=1, overfetch_multiplier=1))
        wide = HybridSearcher(sqlite, index, HybridParams(top_k=1, overfetch_multiplier=NUM_CHUNKS))
        narrow_hit = await narrow.search(token, corpus[0][1], None)
        wide_hit = await wide.search(token, corpus[0][1], None)
        assert len(narrow_hit) == 1
        assert len(wide_hit) == 1
        assert wide_hit[0].source in {HitSource.DENSE, HitSource.SPARSE, HitSource.BOTH}
    finally:
        await sqlite.close()
