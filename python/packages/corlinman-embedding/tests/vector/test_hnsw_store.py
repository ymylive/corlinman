"""HNSW (usearch) wrapper — mirror of `usearch_index.rs` tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from corlinman_embedding.vector.hnsw_store import UsearchIndex


def test_create_add_search() -> None:
    idx = UsearchIndex.create_with_capacity(4, 16)
    assert idx.dim == 4
    assert idx.size == 0

    idx.add(1, [1.0, 0.0, 0.0, 0.0])
    idx.add(2, [0.0, 1.0, 0.0, 0.0])
    idx.add(3, [0.9, 0.1, 0.0, 0.0])
    assert idx.size == 3

    hits = idx.search([1.0, 0.0, 0.0, 0.0], 3)
    assert len(hits) == 3
    assert hits[0][0] == 1
    assert hits[0][1] < 1e-3
    pos = {k: i for i, (k, _) in enumerate(hits)}
    assert pos[3] < pos[2], "key 3 should rank above key 2"


def test_save_and_reload(tmp_path: Path) -> None:
    path = tmp_path / "roundtrip.usearch"

    idx = UsearchIndex.create_with_capacity(3, 8)
    idx.add(42, [0.1, 0.2, 0.3])
    idx.add(99, [0.9, 0.1, 0.0])
    idx.save(path)

    loaded = UsearchIndex.open(path)
    assert loaded.dim == 3
    assert loaded.size == 2
    hits = loaded.search([0.1, 0.2, 0.3], 2)
    assert hits[0][0] == 42


def test_dim_mismatch_is_error() -> None:
    idx = UsearchIndex.create_with_capacity(4, 8)
    with pytest.raises(ValueError, match="dim mismatch"):
        idx.add(1, [1.0, 2.0])
    with pytest.raises(ValueError, match="dim mismatch"):
        idx.search([1.0, 2.0], 1)


def test_search_on_empty_index_returns_empty() -> None:
    idx = UsearchIndex.create_with_capacity(4, 8)
    assert idx.search([0.0, 0.0, 0.0, 1.0], 5) == []


def test_search_k_zero_returns_empty() -> None:
    idx = UsearchIndex.create_with_capacity(2, 4)
    idx.add(1, [1.0, 0.0])
    assert idx.search([1.0, 0.0], 0) == []


def test_open_checked_rejects_wrong_dim(tmp_path: Path) -> None:
    path = tmp_path / "dim.usearch"
    idx = UsearchIndex.create_with_capacity(4, 8)
    idx.save(path)
    with pytest.raises(RuntimeError, match="dim mismatch"):
        UsearchIndex.open_checked(path, 3)
    ok = UsearchIndex.open_checked(path, 4)
    assert ok.dim == 4


def test_upsert_existing_key_replaces_vector() -> None:
    idx = UsearchIndex.create_with_capacity(4, 16)
    idx.upsert(7, [1.0, 0.0, 0.0, 0.0])
    idx.upsert(7, [0.0, 1.0, 0.0, 0.0])
    assert idx.size == 1
    hits = idx.search([0.0, 1.0, 0.0, 0.0], 1)
    assert len(hits) == 1
    assert hits[0][0] == 7
    assert hits[0][1] < 1e-3


def test_upsert_new_key_adds() -> None:
    idx = UsearchIndex.create_with_capacity(3, 8)
    idx.upsert(101, [0.5, 0.5, 0.5])
    assert idx.size == 1
    hits = idx.search([0.5, 0.5, 0.5], 1)
    assert hits[0][0] == 101


async def test_async_search_roundtrip(tmp_path: Path) -> None:
    idx = UsearchIndex.create_with_capacity(2, 4)
    await idx.aadd(1, [1.0, 0.0])
    await idx.aadd(2, [0.0, 1.0])
    await idx.asave(tmp_path / "a.usearch")
    hits = await idx.asearch([1.0, 0.0], 1)
    assert hits[0][0] == 1
