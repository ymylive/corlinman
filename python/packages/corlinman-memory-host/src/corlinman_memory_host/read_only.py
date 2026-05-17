"""Read-only adapter wrapping any :class:`MemoryHost`.

Python port of ``rust/crates/corlinman-memory-host/src/read_only.rs``.

Subagents (Phase 4 W4 D3) inherit their parent's ``memory_host`` so the
child can search the same knowledge sources, but every D3 design
decision says they MUST NOT mutate that store: a delegated child that
learns something new bubbles its findings up to the parent's context,
never into shared memory directly.

This adapter forwards ``query`` + ``health`` to the inner host while
rejecting ``upsert`` / ``delete`` with a tagged
:class:`MemoryHostError` whose message starts with
:data:`READ_ONLY_REJECT_TAG`. Callers can branch on the tag to surface
"subagent attempted write" telemetry rather than confusing the user
with a generic backend failure.

Composes cleanly with :class:`FederatedMemoryHost`: a federated host
wrapped read-only continues to fan-out queries across its members and
merge with the same Reciprocal Rank Fusion logic; only writes are
refused at the outer wrapper before reaching any inner adapter."""

from __future__ import annotations

from corlinman_memory_host.base import MemoryHost
from corlinman_memory_host.types import (
    HealthStatus,
    MemoryDoc,
    MemoryHit,
    MemoryHostError,
    MemoryQuery,
)

# Error message tag returned by ``upsert`` / ``delete`` so callers can
# distinguish a read-only-rejection from a genuine backend failure
# without parsing English prose.
READ_ONLY_REJECT_TAG: str = "memory_host_read_only"


class ReadOnlyMemoryHost(MemoryHost):
    """Wraps any :class:`MemoryHost` and forbids ``upsert`` / ``delete``.

    :meth:`name` prefixes the inner host's name with ``"ro:"`` so
    attribution in :attr:`MemoryHit.source` stays correct without
    colliding with a hypothetical sibling host that happens to share
    the inner's name.
    """

    def __init__(self, inner: MemoryHost) -> None:
        self._inner = inner
        # Cached ``f"ro:{inner_name}"`` so :meth:`name` doesn't allocate
        # per call (mirrors the Rust ``cached_name`` field).
        self._cached_name = f"ro:{inner.name()}"

    def name(self) -> str:
        return self._cached_name

    async def query(self, req: MemoryQuery) -> list[MemoryHit]:
        return await self._inner.query(req)

    async def upsert(self, doc: MemoryDoc) -> str:
        _ = doc
        raise MemoryHostError(
            f"{READ_ONLY_REJECT_TAG}: upsert refused — host {self._inner.name()!r} "
            f"is wrapped read-only (subagent / inherited contexts cannot mutate "
            f"parent memory)"
        )

    async def delete(self, doc_id: str) -> None:
        raise MemoryHostError(
            f"{READ_ONLY_REJECT_TAG}: delete({doc_id}) refused — host "
            f"{self._inner.name()!r} is wrapped read-only"
        )

    async def health(self) -> HealthStatus:
        return await self._inner.health()


__all__ = ["READ_ONLY_REJECT_TAG", "ReadOnlyMemoryHost"]
