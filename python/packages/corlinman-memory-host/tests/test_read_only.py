"""Port of the ``#[cfg(test)] mod tests`` in
``rust/crates/corlinman-memory-host/src/read_only.rs``.

Uses a ``CountingHost`` analogue (the Python equivalent of the Rust
inline ``CountingHost`` test double) to verify that ``query`` forwards
to the inner host while ``upsert`` / ``delete`` raise tagged errors
without ever touching the inner host."""

from __future__ import annotations

import pytest
from corlinman_memory_host import (
    READ_ONLY_REJECT_TAG,
    FederatedMemoryHost,
    FusionStrategy,
    MemoryDoc,
    MemoryHit,
    MemoryHost,
    MemoryHostError,
    MemoryQuery,
    ReadOnlyMemoryHost,
)


class _CountingHost(MemoryHost):
    """Test double: counts how many times each method was invoked + carries
    a canned ``query`` response so we can verify forwarding without a
    real backend. Sufficient for protocol-level wiring tests."""

    def __init__(self, host_name: str, canned_hits: list[MemoryHit]) -> None:
        self._name = host_name
        self._canned_hits = canned_hits
        self.query_calls = 0
        self.upsert_calls = 0
        self.delete_calls = 0

    def name(self) -> str:
        return self._name

    async def query(self, req: MemoryQuery) -> list[MemoryHit]:
        _ = req
        self.query_calls += 1
        return list(self._canned_hits)

    async def upsert(self, doc: MemoryDoc) -> str:
        _ = doc
        self.upsert_calls += 1
        return "inner-id"

    async def delete(self, doc_id: str) -> None:
        _ = doc_id
        self.delete_calls += 1


def _hit(hit_id: str, source: str, score: float) -> MemoryHit:
    return MemoryHit(
        id=hit_id,
        content=f"body of {hit_id}",
        score=score,
        source=source,
        metadata=None,
    )


def _query(text: str) -> MemoryQuery:
    return MemoryQuery(text=text, top_k=5)


def test_name_is_prefixed_with_ro() -> None:
    inner = _CountingHost("local-kb", [])
    ro = ReadOnlyMemoryHost(inner)
    assert ro.name() == "ro:local-kb"


async def test_query_forwards_to_inner_and_returns_hits() -> None:
    canned = [_hit("doc-1", "local-kb", 0.9)]
    inner = _CountingHost("local-kb", canned)
    ro = ReadOnlyMemoryHost(inner)

    hits = await ro.query(_query("anything"))

    assert len(hits) == 1
    assert hits[0].id == "doc-1"
    assert inner.query_calls == 1
    assert inner.upsert_calls == 0
    assert inner.delete_calls == 0


async def test_upsert_returns_tagged_error_and_does_not_call_inner() -> None:
    inner = _CountingHost("local-kb", [])
    ro = ReadOnlyMemoryHost(inner)

    with pytest.raises(MemoryHostError) as excinfo:
        await ro.upsert(MemoryDoc(content="hello"))

    assert READ_ONLY_REJECT_TAG in str(excinfo.value)
    assert "local-kb" in str(excinfo.value)
    assert inner.upsert_calls == 0


async def test_delete_returns_tagged_error_and_does_not_call_inner() -> None:
    inner = _CountingHost("local-kb", [])
    ro = ReadOnlyMemoryHost(inner)

    with pytest.raises(MemoryHostError) as excinfo:
        await ro.delete("doc-1")

    assert READ_ONLY_REJECT_TAG in str(excinfo.value)
    assert "doc-1" in str(excinfo.value)
    assert inner.delete_calls == 0


async def test_federated_host_wrapped_readonly_still_does_rrf() -> None:
    """RRF roundtrip: wrapping a ``FederatedMemoryHost`` read-only must
    preserve the fan-out + fusion behaviour for queries; only writes
    get the tagged-rejection treatment."""
    host_a = _CountingHost(
        "kb-a", [_hit("a-1", "kb-a", 1.0), _hit("a-2", "kb-a", 0.7)]
    )
    host_b = _CountingHost(
        "kb-b", [_hit("b-1", "kb-b", 0.95), _hit("a-1", "kb-b", 0.4)]
    )

    federated = FederatedMemoryHost(
        "fed", [host_a, host_b], FusionStrategy.rrf(k=60.0)
    )
    ro = ReadOnlyMemoryHost(federated)

    hits = await ro.query(_query("anything"))

    # Query path: same merging behaviour as the underlying federated
    # host.
    assert hits, "RRF should produce hits through the wrapper"
    ids = [h.id for h in hits]
    assert "a-1" in ids

    # Write path: refused regardless of nested host shape.
    with pytest.raises(MemoryHostError) as excinfo:
        await ro.upsert(MemoryDoc(content="shouldn't reach federation"))
    assert READ_ONLY_REJECT_TAG in str(excinfo.value)
