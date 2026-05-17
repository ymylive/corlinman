"""Port of the ``#[cfg(test)] mod tests`` in
``rust/crates/corlinman-memory-host/src/federation.rs``.

Uses a deterministic ``MockHost`` (analogue of the Rust struct of the
same name): canned hits, optional delay to verify the fan-out is
parallel, optional failure to verify the per-host skip path."""

from __future__ import annotations

import asyncio

import pytest
from corlinman_memory_host import (
    FederatedMemoryHost,
    FusionStrategy,
    MemoryDoc,
    MemoryHit,
    MemoryHost,
    MemoryHostError,
    MemoryQuery,
)


class _MockHost(MemoryHost):
    """Deterministic mock host that returns a pre-canned list."""

    def __init__(
        self,
        host_name: str,
        hits: list[MemoryHit],
        *,
        delay: float | None = None,
        fail: bool = False,
    ) -> None:
        self._name = host_name
        self._hits = hits
        self._delay = delay
        self._fail = fail

    def name(self) -> str:
        return self._name

    async def query(self, req: MemoryQuery) -> list[MemoryHit]:
        _ = req
        if self._delay is not None:
            await asyncio.sleep(self._delay)
        if self._fail:
            raise MemoryHostError("mock failure")
        # Return a defensive copy — the federator mutates ``hit.score``
        # in place when fusing, and we don't want that to leak across
        # tests.
        return [
            MemoryHit(
                id=h.id,
                content=h.content,
                score=h.score,
                source=h.source,
                metadata=h.metadata,
            )
            for h in self._hits
        ]

    async def upsert(self, doc: MemoryDoc) -> str:
        _ = doc
        return "noop"

    async def delete(self, doc_id: str) -> None:
        _ = doc_id


def _hit(source: str, hit_id: str, score: float) -> MemoryHit:
    return MemoryHit(
        id=hit_id,
        content=f"{source}:{hit_id}",
        score=score,
        source=source,
        metadata=None,
    )


async def test_rrf_merges_two_hosts_and_ranks_overlap_higher() -> None:
    host_a = _MockHost(
        "a",
        [_hit("a", "a1", 10.0), _hit("a", "a2", 9.0), _hit("a", "shared", 8.0)],
    )
    host_b = _MockHost(
        "b",
        [_hit("b", "shared", 7.0), _hit("b", "b1", 6.0)],
    )

    fed = FederatedMemoryHost("fed", [host_a, host_b], FusionStrategy.rrf(k=60.0))
    merged = await fed.query(MemoryQuery(text="x", top_k=10))

    # 3 from host_a + 2 from host_b = 5 unique (source, id) keys.
    assert len(merged) == 5

    # Rank-0 items get 1/(60+1) each. Tie-break is (source, id) ascending,
    # so host_a's rank-0 ("a1") comes before host_b's rank-0 ("shared").
    assert merged[0].source == "a"
    assert merged[0].id == "a1"
    assert merged[1].source == "b"
    assert merged[1].id == "shared"

    # Scores are monotonically non-increasing.
    for i in range(len(merged) - 1):
        assert merged[i].score >= merged[i + 1].score, f"order broken: {merged}"


async def test_failing_host_is_skipped_and_healthy_host_still_returns() -> None:
    good = _MockHost("good", [_hit("good", "g1", 5.0), _hit("good", "g2", 4.0)])
    bad = _MockHost("bad", [], fail=True)

    fed = FederatedMemoryHost.with_rrf("fed", [good, bad])
    merged = await fed.query(MemoryQuery(text="x", top_k=5))

    assert len(merged) == 2
    assert all(h.source == "good" for h in merged)


async def test_all_failing_returns_empty_not_error() -> None:
    f1 = _MockHost("f1", [], fail=True)
    f2 = _MockHost("f2", [], fail=True)
    fed = FederatedMemoryHost.with_rrf("fed", [f1, f2])

    merged = await fed.query(MemoryQuery(text="x", top_k=5))
    assert merged == []


async def test_top_k_truncates_fused_list() -> None:
    host = _MockHost(
        "h",
        [
            _hit("h", "1", 5.0),
            _hit("h", "2", 4.0),
            _hit("h", "3", 3.0),
            _hit("h", "4", 2.0),
        ],
    )
    fed = FederatedMemoryHost.with_rrf("fed", [host])
    merged = await fed.query(MemoryQuery(text="x", top_k=2))
    assert len(merged) == 2


async def test_slow_host_alongside_fast_host_does_not_drop_fast_results() -> None:
    """Guards the "fan-out is parallel" property.

    Same intent as the Rust test: the slow host takes a moment but the
    fast host's results are still merged in. Per-host timeouts belong on
    the individual host; the federator's contract is only "keep going if
    one peer errors or returns empty"."""
    fast = _MockHost("fast", [_hit("fast", "x", 1.0)])
    slow = _MockHost("slow", [_hit("slow", "y", 1.0)], delay=0.05)
    fed = FederatedMemoryHost.with_rrf("fed", [fast, slow])

    merged = await fed.query(MemoryQuery(text="x", top_k=10))
    sources = [h.source for h in merged]
    assert "fast" in sources
    assert "slow" in sources


async def test_upsert_and_delete_are_rejected_on_federation() -> None:
    fed = FederatedMemoryHost.with_rrf("fed", [])
    with pytest.raises(MemoryHostError):
        await fed.upsert(MemoryDoc(content="c"))
    with pytest.raises(MemoryHostError):
        await fed.delete("id")
