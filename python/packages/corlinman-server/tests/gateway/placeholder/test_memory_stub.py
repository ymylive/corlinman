"""Tests for :mod:`corlinman_server.gateway.placeholder.memory_stub`.

The real :class:`MemoryHost` adapters live in ``corlinman-memory-host``
(a soft sibling dep). We stub the host inline so the tests run even when
that workspace member isn't installed — the production deployment will
declare the dep and the resolver will route through the real protocol.
"""

from __future__ import annotations

import pytest

mh_mod = pytest.importorskip("corlinman_memory_host")

from corlinman_memory_host import MemoryHit, MemoryHost, MemoryQuery  # noqa: E402

from corlinman_server.gateway.placeholder.memory_stub import (  # noqa: E402
    DEFAULT_MEMORY_NAMESPACE,
    DEFAULT_TOP_K,
    MemoryResolver,
)


class _StubHost(MemoryHost):
    def __init__(self, hits: list[MemoryHit]) -> None:
        self._hits = hits
        self.queries: list[MemoryQuery] = []

    def name(self) -> str:
        return "stub"

    async def query(self, req: MemoryQuery) -> list[MemoryHit]:
        self.queries.append(req)
        return list(self._hits)

    async def upsert(self, doc):  # type: ignore[no-untyped-def]
        raise RuntimeError("not used")

    async def delete(self, doc_id: str) -> None:
        raise RuntimeError("not used")


def test_constants_match_rust_contract() -> None:
    assert DEFAULT_MEMORY_NAMESPACE == "agent-brain"
    assert DEFAULT_TOP_K == 5


async def test_resolver_queries_agent_brain_namespace_and_renders_hits() -> None:
    host = _StubHost(
        [
            MemoryHit(
                id="m1",
                content="Use PostgreSQL for durable state",
                score=0.9,
                source="local-kb",
            )
        ]
    )
    resolver = MemoryResolver(host).with_top_k(3)

    out = await resolver.resolve(" durable state ", ctx=None)

    assert "Use PostgreSQL for durable state" in out
    assert "local-kb:m1" in out
    assert len(host.queries) == 1
    assert host.queries[0].text == "durable state"
    assert host.queries[0].top_k == 3
    assert host.queries[0].namespace == "agent-brain"


async def test_empty_query_renders_empty_without_calling_host() -> None:
    host = _StubHost([])
    resolver = MemoryResolver(host)

    out = await resolver.resolve("   ", ctx=None)
    assert out == ""
    assert host.queries == []


async def test_multiple_hits_render_as_bullet_list() -> None:
    host = _StubHost(
        [
            MemoryHit(id="a", content="one", score=0.5, source="kb"),
            MemoryHit(id="b", content="two", score=0.4, source="kb"),
        ]
    )
    resolver = MemoryResolver(host)

    out = await resolver.resolve("anything", ctx=None)
    assert out == "- one (kb:a)\n- two (kb:b)"


async def test_top_k_clamp_floor_is_one() -> None:
    host = _StubHost([])
    resolver = MemoryResolver(host, top_k=0)
    assert resolver.top_k == 1


def test_with_namespace_returns_new_instance() -> None:
    host = _StubHost([])
    base = MemoryResolver(host)
    other = base.with_namespace("custom")
    assert other.namespace == "custom"
    assert base.namespace == DEFAULT_MEMORY_NAMESPACE
