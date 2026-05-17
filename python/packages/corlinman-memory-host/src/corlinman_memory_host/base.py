"""The :class:`MemoryHost` async protocol every adapter implements.

Python port of the ``MemoryHost`` trait declared in
``rust/crates/corlinman-memory-host/src/lib.rs``. The Rust trait is
``Send + Sync`` so it can be shared across tokio tasks; the asyncio
analogue is "callable from any task" — adapters in this package
hold only thread-safe state (a single ``aiosqlite.Connection`` guarded
by a lock, or an ``httpx.AsyncClient``)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from corlinman_memory_host.types import (
    HealthStatus,
    MemoryDoc,
    MemoryHit,
    MemoryHostError,
    MemoryQuery,
)


class MemoryHost(ABC):
    """A pluggable memory source.

    All methods are ``async``. Implementations should never block the
    event loop — even for slow remote backends — by deferring I/O to
    the underlying async driver (``httpx`` / ``aiosqlite``).
    """

    @abstractmethod
    def name(self) -> str:
        """Unique identifier (e.g. ``"local-kb"`` / ``"notion"``).

        Returned on every :class:`MemoryHit` so downstream code can
        attribute a hit to its originating host.
        """

    @abstractmethod
    async def query(self, req: MemoryQuery) -> list[MemoryHit]:
        """Query top-k semantically relevant hits."""

    @abstractmethod
    async def upsert(self, doc: MemoryDoc) -> str:
        """Upsert a document; returns the host-assigned id."""

    @abstractmethod
    async def delete(self, doc_id: str) -> None:
        """Delete by id."""

    async def get(self, doc_id: str) -> MemoryHit | None:
        """Fetch a single document by id.

        Returns ``None`` when the id is well-formed but unknown to this
        host; raises :class:`MemoryHostError` on transport / decode
        failures only.

        Default impl raises ``MemoryHostError`` so adapters that don't
        implement read-by-id keep the same shape as the Rust default.
        Phase 4 W3 C1 (MCP ``resources/read``) needs this hook;
        :class:`LocalSqliteHost` overrides it.
        """
        _ = doc_id  # silence unused
        raise MemoryHostError("MemoryHost.get is not implemented for this adapter")

    async def health(self) -> HealthStatus:
        """Optional health check for observability. Default is ``Ok``."""
        return HealthStatus.ok()


__all__ = ["MemoryHost"]
