"""Port of the ``#[cfg(test)] mod tests`` in
``rust/crates/corlinman-memory-host/src/local_sqlite.rs``.

Every Rust test case has a 1:1 Python counterpart with the same
assertions and the same data shape, modulo the Rust ``Arc<SqliteStore>``
plumbing (Python opens the store via :meth:`LocalSqliteHost.open` and
closes it in the fixture's finalizer)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from corlinman_memory_host import (
    LocalSqliteHost,
    MemoryDoc,
    MemoryQuery,
)


@pytest.fixture
async def host(tmp_path: Path) -> AsyncIterator[LocalSqliteHost]:
    h = await LocalSqliteHost.open("local-kb", tmp_path / "kb.sqlite")
    try:
        yield h
    finally:
        await h.close()


async def test_upsert_then_query_roundtrip(host: LocalSqliteHost) -> None:
    doc_id = await host.upsert(
        MemoryDoc(
            content="the lazy fox jumps over dogs",
            metadata={"author": "x"},
        )
    )
    assert doc_id

    hits = await host.query(MemoryQuery(text="lazy fox", top_k=3))
    assert len(hits) == 1
    assert hits[0].id == doc_id
    assert hits[0].source == "local-kb"
    assert hits[0].score > 0.0
    assert "lazy fox" in hits[0].content


async def test_namespace_filter_scopes_results(host: LocalSqliteHost) -> None:
    id_a = await host.upsert(
        MemoryDoc(content="alpha document body", namespace="diary")
    )
    _id_b = await host.upsert(
        MemoryDoc(content="alpha document body", namespace="papers")
    )

    hits = await host.query(
        MemoryQuery(text="alpha", top_k=10, namespace="diary")
    )
    assert len(hits) == 1
    assert hits[0].id == id_a


async def test_query_preserves_upserted_metadata(host: LocalSqliteHost) -> None:
    await host.upsert(
        MemoryDoc(
            content="alpha graph node",
            metadata={
                "node_id": "kn-a",
                "title": "Alpha Node",
                "links": ["kn-b"],
                "related_nodes": ["Beta Node"],
            },
            namespace="agent-brain",
        )
    )

    hits = await host.query(
        MemoryQuery(text="alpha", top_k=3, namespace="agent-brain")
    )
    assert len(hits) == 1
    assert hits[0].metadata["node_id"] == "kn-a"
    assert hits[0].metadata["title"] == "Alpha Node"
    assert hits[0].metadata["links"] == ["kn-b"]
    assert hits[0].metadata["related_nodes"] == ["Beta Node"]


async def test_query_expands_one_hop_links_after_bm25_seed(
    host: LocalSqliteHost,
) -> None:
    id_a = await host.upsert(
        MemoryDoc(
            content="alpha seed memory",
            metadata={"node_id": "kn-a", "title": "Alpha", "links": ["kn-b"]},
            namespace="agent-brain",
        )
    )
    id_b = await host.upsert(
        MemoryDoc(
            content="beta linked context without query term",
            metadata={"node_id": "kn-b", "title": "Beta", "links": []},
            namespace="agent-brain",
        )
    )
    id_c = await host.upsert(
        MemoryDoc(
            content="gamma backlink context without query term",
            metadata={"node_id": "kn-c", "title": "Gamma", "links": ["kn-a"]},
            namespace="agent-brain",
        )
    )

    hits = await host.query(
        MemoryQuery(text="alpha", top_k=3, namespace="agent-brain")
    )
    ids = [h.id for h in hits]
    assert ids == [id_a, id_b, id_c]
    assert hits[0].metadata["graph_expanded"] is False
    assert hits[1].metadata["graph_expanded"] is True
    assert hits[2].metadata["graph_expanded"] is True


async def test_query_dedupes_by_node_id_and_host_metadata_wins(
    host: LocalSqliteHost,
) -> None:
    id_a = await host.upsert(
        MemoryDoc(
            content="alpha duplicate first",
            metadata={
                "node_id": "kn-a",
                "title": "Alpha",
                "namespace": "spoofed",
                "graph_expanded": True,
            },
            namespace="agent-brain",
        )
    )
    _id_dup = await host.upsert(
        MemoryDoc(
            content="alpha duplicate second",
            metadata={"node_id": "kn-a", "title": "Alpha duplicate"},
            namespace="agent-brain",
        )
    )

    hits = await host.query(
        MemoryQuery(text="alpha duplicate", top_k=5, namespace="agent-brain")
    )
    assert len(hits) == 1
    assert hits[0].id == id_a
    # Host metadata wins on conflict — the per-call host_base overrides
    # the upserted metadata's ``namespace`` / ``graph_expanded`` keys.
    assert hits[0].metadata["namespace"] == "agent-brain"
    assert hits[0].metadata["graph_expanded"] is False


async def test_query_dedupes_before_applying_top_k_budget(
    host: LocalSqliteHost,
) -> None:
    id_a = await host.upsert(
        MemoryDoc(
            content="alpha duplicate first",
            metadata={"node_id": "kn-a", "title": "Alpha", "links": ["kn-b"]},
            namespace="agent-brain",
        )
    )
    _id_dup = await host.upsert(
        MemoryDoc(
            content="alpha duplicate second",
            metadata={
                "node_id": "kn-a",
                "title": "Alpha duplicate",
                "links": [],
            },
            namespace="agent-brain",
        )
    )
    id_b = await host.upsert(
        MemoryDoc(
            content="beta linked context without query term",
            metadata={"node_id": "kn-b", "title": "Beta", "links": []},
            namespace="agent-brain",
        )
    )

    hits = await host.query(
        MemoryQuery(text="alpha duplicate", top_k=2, namespace="agent-brain")
    )
    ids = [h.id for h in hits]
    assert ids == [id_a, id_b]


async def test_delete_removes_hit(host: LocalSqliteHost) -> None:
    doc_id = await host.upsert(MemoryDoc(content="ephemeral note"))
    await host.delete(doc_id)

    hits = await host.query(MemoryQuery(text="ephemeral", top_k=5))
    assert hits == []


async def test_get_round_trips_upserted_doc(host: LocalSqliteHost) -> None:
    doc_id = await host.upsert(
        MemoryDoc(content="the quick brown fox", namespace="notes")
    )

    hit = await host.get(doc_id)
    assert hit is not None
    assert hit.id == doc_id
    assert hit.content == "the quick brown fox"
    assert hit.source == "local-kb"
    # Score is the "direct lookup" sentinel (1.0).
    assert abs(hit.score - 1.0) < 1e-6
    assert hit.metadata["namespace"] == "notes"


async def test_get_unknown_id_returns_none(host: LocalSqliteHost) -> None:
    # Numeric but unused id.
    assert await host.get("999999") is None
    # Non-numeric id maps to "unknown" too — lenient caller-decides
    # contract, same as the Rust impl.
    assert await host.get("not-a-number") is None


async def test_empty_query_is_empty_result(host: LocalSqliteHost) -> None:
    hits = await host.query(MemoryQuery(text="", top_k=3))
    assert hits == []
