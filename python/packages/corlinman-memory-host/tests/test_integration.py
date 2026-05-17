"""Port of ``rust/crates/corlinman-memory-host/tests/integration.rs``.

Black-box sanity check that all adapters compose through the
:class:`MemoryHost` protocol — two local SQLite hosts behind one
:class:`FederatedMemoryHost`."""

from __future__ import annotations

from pathlib import Path

from corlinman_memory_host import (
    FederatedMemoryHost,
    LocalSqliteHost,
    MemoryDoc,
    MemoryQuery,
)


async def test_federation_over_two_local_sqlite_hosts(tmp_path: Path) -> None:
    host_a = await LocalSqliteHost.open("kb-a", tmp_path / "a.sqlite")
    host_b = await LocalSqliteHost.open("kb-b", tmp_path / "b.sqlite")
    try:
        await host_a.upsert(MemoryDoc(content="shared token alpha only in kb-a"))
        await host_b.upsert(MemoryDoc(content="shared token alpha also in kb-b"))

        fed = FederatedMemoryHost.with_rrf("fed", [host_a, host_b])
        hits = await fed.query(MemoryQuery(text="alpha", top_k=5))

        sources = {h.source for h in hits}
        assert "kb-a" in sources
        assert "kb-b" in sources
    finally:
        await host_a.close()
        await host_b.close()
