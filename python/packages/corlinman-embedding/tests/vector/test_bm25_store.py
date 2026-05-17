"""SqliteStore — mirror of the RAG-relevant `sqlite.rs` tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from corlinman_embedding.vector.bm25_store import (
    SCHEMA_SQL,
    SqliteStore,
    blob_to_f32_vec,
    f32_slice_to_blob,
)


@pytest.fixture
async def store(tmp_path: Path):
    s = await SqliteStore.open(tmp_path / "kb.sqlite")
    try:
        yield s
    finally:
        await s.close()


# ---------------------------------------------------------------------------
# BLOB helpers
# ---------------------------------------------------------------------------


def test_f32_blob_roundtrip() -> None:
    v = [1.0, -2.5, 42.125, 0.0]
    blob = f32_slice_to_blob(v)
    assert len(blob) == len(v) * 4
    back = blob_to_f32_vec(blob)
    assert back == v


def test_blob_wrong_length_rejected() -> None:
    assert blob_to_f32_vec(b"\x01\x02\x03") is None


def test_blob_none_returns_none() -> None:
    assert blob_to_f32_vec(None) is None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_schema_sql_not_empty() -> None:
    assert "CREATE TABLE" in SCHEMA_SQL
    assert "chunks_fts" in SCHEMA_SQL
    assert "tag_nodes" in SCHEMA_SQL
    assert "tag_node_id" in SCHEMA_SQL
    assert "chunk_epa" in SCHEMA_SQL
    assert "namespace TEXT NOT NULL DEFAULT 'general'" in SCHEMA_SQL


async def test_open_creates_schema(store: SqliteStore) -> None:
    for t in ("files", "chunks", "kv_store", "chunks_fts", "chunk_tags", "tag_nodes", "chunk_epa"):
        assert await store.table_exists(t), f"table {t} missing"


# ---------------------------------------------------------------------------
# BM25 / FTS5
# ---------------------------------------------------------------------------


async def test_bm25_search_returns_matching_rows(store: SqliteStore) -> None:
    file_id = await store.insert_file("doc.md", "default", "h", 0, 0)
    await store.insert_chunk(file_id, 0, "the quick brown fox jumps", None, "general")
    target = await store.insert_chunk(file_id, 1, "lazy dog sleeps in the sun", None, "general")
    await store.insert_chunk(file_id, 2, "unrelated content about cats", None, "general")

    hits = await store.search_bm25("lazy dog", 5)
    assert hits, "BM25 should return matches"
    assert hits[0][0] == target
    assert hits[0][1] > 0.0


async def test_bm25_empty_query_returns_empty(store: SqliteStore) -> None:
    assert await store.search_bm25("   ", 5) == []
    assert await store.search_bm25("anything", 0) == []


async def test_fts_trigger_keeps_index_in_sync_on_delete(store: SqliteStore) -> None:
    file_id = await store.insert_file("d.md", "default", "h", 0, 0)
    await store.insert_chunk(file_id, 0, "alpha bravo charlie", None, "general")
    assert len(await store.search_bm25("alpha", 5)) == 1

    await store.connection.execute("DELETE FROM files WHERE id = ?", (file_id,))
    await store.connection.commit()
    assert await store.search_bm25("alpha", 5) == []


async def test_rebuild_fts_populates_rows_inserted_outside_triggers(
    store: SqliteStore,
) -> None:
    file_id = await store.insert_file("d.md", "default", "h", 0, 0)
    await store.insert_chunk(file_id, 0, "hello rebuild world", None, "general")
    # Nuke FTS contents.
    await store.connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('delete-all')")
    await store.connection.commit()
    assert await store.search_bm25("rebuild", 5) == []

    await store.rebuild_fts()
    hits = await store.search_bm25("rebuild", 5)
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# chunks / files
# ---------------------------------------------------------------------------


async def test_empty_lists_are_empty(store: SqliteStore) -> None:
    assert await store.list_files() == []
    assert await store.count_chunks() == 0


async def test_insert_and_query_chunks(store: SqliteStore) -> None:
    file_id = await store.insert_file(
        "公共/2026-04-20.md", "公共", "deadbeef", 1_700_000_000, 1024
    )
    v1 = [0.1, 0.2, 0.3]
    v2 = [0.4, 0.5, 0.6]
    c1 = await store.insert_chunk(file_id, 0, "hello world", v1, "general")
    c2 = await store.insert_chunk(file_id, 1, "second chunk", v2, "general")

    got = await store.get_chunks(file_id)
    assert len(got) == 2
    assert got[0].id == c1
    assert got[0].content == "hello world"
    # Vector roundtrip — f32 ⇒ may have minor representation drift.
    assert got[0].vector is not None
    assert len(got[0].vector) == 3
    assert all(abs(a - b) < 1e-6 for a, b in zip(got[0].vector, v1))

    got = await store.query_chunks_by_ids([c2, c1])
    assert [c.id for c in got] == [c2, c1]
    assert await store.count_chunks() == 2


async def test_query_chunks_by_ids_empty_input_is_empty_output(store: SqliteStore) -> None:
    assert await store.query_chunks_by_ids([]) == []


async def test_search_bm25_with_filter_restricts_hits(store: SqliteStore) -> None:
    file_id = await store.insert_file("t.md", "default", "h", 0, 0)
    a = await store.insert_chunk(file_id, 0, "rust backend content", None, "general")
    b = await store.insert_chunk(file_id, 1, "rust frontend content", None, "general")
    await store.insert_chunk(file_id, 2, "untagged note", None, "general")

    # No filter ⇒ picks up both "rust …" chunks.
    hits = await store.search_bm25_with_filter("rust", 10, None)
    assert len(hits) == 2

    # Whitelist only chunk a.
    hits = await store.search_bm25_with_filter("rust", 10, [a])
    assert len(hits) == 1
    assert hits[0][0] == a

    # Empty whitelist ⇒ empty.
    hits = await store.search_bm25_with_filter("rust", 10, [])
    assert hits == []
    # Suppress unused-var warning
    assert b > 0


# ---------------------------------------------------------------------------
# tag_nodes / chunk_tags
# ---------------------------------------------------------------------------


async def _seed_tagged_chunks(store: SqliteStore) -> tuple[int, int, int]:
    file_id = await store.insert_file("t.md", "default", "h", 0, 0)
    a = await store.insert_chunk(file_id, 0, "rust backend content", None, "general")
    b = await store.insert_chunk(file_id, 1, "rust frontend content", None, "general")
    c = await store.insert_chunk(file_id, 2, "untagged note", None, "general")
    await store.insert_tag(a, "rust")
    await store.insert_tag(a, "backend")
    await store.insert_tag(b, "rust")
    await store.insert_tag(b, "frontend")
    return a, b, c


async def test_insert_and_get_tags_roundtrip(store: SqliteStore) -> None:
    a, _b, c = await _seed_tagged_chunks(store)
    assert await store.get_tags(a) == ["backend", "rust"]
    assert await store.get_tags(c) == []
    # Idempotency.
    await store.insert_tag(a, "rust")
    assert len(await store.get_tags(a)) == 2


async def test_count_files_and_tags(store: SqliteStore) -> None:
    assert await store.count_files() == 0
    assert await store.count_tags() == 0
    await _seed_tagged_chunks(store)
    assert await store.count_files() == 1
    # Distinct tags: rust, backend, frontend.
    assert await store.count_tags() == 3


async def test_ensure_tag_path_builds_hierarchy(store: SqliteStore) -> None:
    # Three segments — three nodes upserted, leaf id returned.
    leaf = await store.ensure_tag_path("role.protagonist.voice")
    # Re-running is idempotent + returns the same leaf id.
    leaf_again = await store.ensure_tag_path("role.protagonist.voice")
    assert leaf == leaf_again


async def test_ensure_tag_path_rejects_invalid(store: SqliteStore) -> None:
    with pytest.raises(ValueError):
        await store.ensure_tag_path("")
    with pytest.raises(ValueError):
        await store.ensure_tag_path("role..voice")


async def test_filter_chunk_ids_by_tag_subtree(store: SqliteStore) -> None:
    file_id = await store.insert_file("st.md", "default", "h", 0, 0)
    a = await store.insert_chunk(file_id, 0, "alpha", None, "general")
    b = await store.insert_chunk(file_id, 1, "bravo", None, "general")
    c = await store.insert_chunk(file_id, 2, "charlie", None, "general")
    await store.attach_chunk_to_tag_path(a, "role.protagonist.voice")
    await store.attach_chunk_to_tag_path(b, "role.antagonist")
    await store.attach_chunk_to_tag_path(c, "mood.calm")

    role = await store.filter_chunk_ids_by_tag_subtree("role")
    assert set(role) == {a, b}
    mood = await store.filter_chunk_ids_by_tag_subtree("mood")
    assert mood == [c]


async def test_filter_chunk_ids_by_tags_required(store: SqliteStore) -> None:
    a, _b, _c = await _seed_tagged_chunks(store)
    ids = await store.filter_chunk_ids_by_tags(required=["rust", "backend"])
    assert ids == [a]


async def test_filter_chunk_ids_by_tags_any_of(store: SqliteStore) -> None:
    a, b, _c = await _seed_tagged_chunks(store)
    ids = await store.filter_chunk_ids_by_tags(any_of=["backend", "frontend"])
    assert set(ids) == {a, b}


async def test_filter_chunk_ids_by_tags_excluded(store: SqliteStore) -> None:
    a, _b, c = await _seed_tagged_chunks(store)
    ids = await store.filter_chunk_ids_by_tags(excluded=["frontend"])
    # Excludes b (frontend); keeps a + c.
    assert set(ids) == {a, c}


async def test_filter_chunk_ids_by_tags_empty_means_all(store: SqliteStore) -> None:
    a, b, c = await _seed_tagged_chunks(store)
    ids = await store.filter_chunk_ids_by_tags()
    assert set(ids) == {a, b, c}


# ---------------------------------------------------------------------------
# chunk_epa
# ---------------------------------------------------------------------------


async def test_chunk_epa_roundtrip(store: SqliteStore) -> None:
    file_id = await store.insert_file("e.md", "default", "h", 0, 0)
    chunk_id = await store.insert_chunk(file_id, 0, "epa target", None, "general")
    assert await store.get_chunk_epa(chunk_id) is None

    await store.upsert_chunk_epa(chunk_id, [0.5, 0.1], 0.3, 0.9)
    row = await store.get_chunk_epa(chunk_id)
    assert row is not None
    assert row.chunk_id == chunk_id
    assert all(abs(a - b) < 1e-6 for a, b in zip(row.projections, [0.5, 0.1]))
    assert abs(row.entropy - 0.3) < 1e-6
    assert abs(row.logic_depth - 0.9) < 1e-6


async def test_chunk_epa_upsert_replaces(store: SqliteStore) -> None:
    file_id = await store.insert_file("e.md", "default", "h", 0, 0)
    chunk_id = await store.insert_chunk(file_id, 0, "epa target", None, "general")
    await store.upsert_chunk_epa(chunk_id, [0.1], 0.0, 0.0)
    await store.upsert_chunk_epa(chunk_id, [0.9, 0.8], 0.5, 0.5)
    row = await store.get_chunk_epa(chunk_id)
    assert row is not None
    assert len(row.projections) == 2


# ---------------------------------------------------------------------------
# namespaces
# ---------------------------------------------------------------------------


async def test_list_namespaces_counts_rows_per_namespace(store: SqliteStore) -> None:
    file_id = await store.insert_file("n.md", "default", "h", 0, 0)
    await store.insert_chunk(file_id, 0, "a", None, "general")
    await store.insert_chunk(file_id, 1, "b", None, "general")
    await store.insert_chunk(file_id, 2, "c", None, "diary:a")
    nss = await store.list_namespaces()
    assert nss == [("diary:a", 1), ("general", 2)]


async def test_filter_chunk_ids_by_namespace(store: SqliteStore) -> None:
    file_id = await store.insert_file("n.md", "default", "h", 0, 0)
    a = await store.insert_chunk(file_id, 0, "a", None, "general")
    b = await store.insert_chunk(file_id, 1, "b", None, "diary:a")
    ids_general = await store.filter_chunk_ids_by_namespace(["general"])
    assert ids_general == [a]
    ids_diary = await store.filter_chunk_ids_by_namespace(["diary:a"])
    assert ids_diary == [b]
    ids_all_empty = await store.filter_chunk_ids_by_namespace([])
    assert set(ids_all_empty) == {a, b}


# ---------------------------------------------------------------------------
# kv_store
# ---------------------------------------------------------------------------


async def test_kv_roundtrip(store: SqliteStore) -> None:
    assert await store.kv_get("missing") is None
    await store.kv_set("schema_version", "6")
    assert await store.kv_get("schema_version") == "6"
    # Overwrite.
    await store.kv_set("schema_version", "7")
    assert await store.kv_get("schema_version") == "7"


async def test_reopen_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "kb.sqlite"
    s1 = await SqliteStore.open(path)
    await s1.insert_file("a.md", "d", "h", 0, 0)
    await s1.kv_set("schema_version", "6")
    await s1.close()

    s2 = await SqliteStore.open(path)
    try:
        assert len(await s2.list_files()) == 1
        assert await s2.kv_get("schema_version") == "6"
    finally:
        await s2.close()


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_chunk_by_id_drops_from_fts(store: SqliteStore) -> None:
    file_id = await store.insert_file("d.md", "default", "h", 0, 0)
    cid = await store.insert_chunk(file_id, 0, "deleteme target", None, "general")
    assert len(await store.search_bm25("deleteme", 5)) == 1
    n = await store.delete_chunk_by_id(cid)
    assert n == 1
    assert await store.search_bm25("deleteme", 5) == []
