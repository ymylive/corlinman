"""corlinman-memory-host — unified memory-source interface.

Python port of ``rust/crates/corlinman-memory-host``. Defines the
:class:`MemoryHost` protocol so external knowledge sources can plug
into hybrid search behind one async contract.

Adapters:

- :class:`LocalSqliteHost` — local SQLite + FTS5 BM25 store (aiosqlite).
- :class:`RemoteHttpHost` — JSON-over-HTTP client (httpx).
- :class:`FederatedMemoryHost` — fan-out + Reciprocal Rank Fusion merge.
- :class:`ReadOnlyMemoryHost` — wraps any host, refuses writes (used by
  subagents that inherit a parent's memory).
"""

from __future__ import annotations

from corlinman_memory_host.base import MemoryHost
from corlinman_memory_host.federation import FederatedMemoryHost, FusionStrategy
from corlinman_memory_host.local_sqlite import LocalSqliteHost
from corlinman_memory_host.read_only import READ_ONLY_REJECT_TAG, ReadOnlyMemoryHost
from corlinman_memory_host.remote_http import RemoteHttpHost
from corlinman_memory_host.types import (
    HealthStatus,
    MemoryDoc,
    MemoryFilter,
    MemoryHit,
    MemoryHostError,
    MemoryQuery,
)

__all__: list[str] = [
    "READ_ONLY_REJECT_TAG",
    "FederatedMemoryHost",
    "FusionStrategy",
    "HealthStatus",
    "LocalSqliteHost",
    "MemoryDoc",
    "MemoryFilter",
    "MemoryHit",
    "MemoryHost",
    "MemoryHostError",
    "MemoryQuery",
    "ReadOnlyMemoryHost",
    "RemoteHttpHost",
]
