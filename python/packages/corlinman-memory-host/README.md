# corlinman-memory-host

Unified memory-source interface for corlinman hybrid search.

Python port of the Rust crate `rust/crates/corlinman-memory-host`. Defines
the `MemoryHost` protocol so external knowledge sources (Notion, remote
Pinecone, enterprise wiki, the native SQLite store) can plug into hybrid
search behind one contract.

Four adapters ship in this package:

- `LocalSqliteHost` — self-contained SQLite + FTS5 (BM25) store with
  metadata + one-hop graph expansion. Uses `aiosqlite`.
- `RemoteHttpHost` — speaks the minimal JSON protocol over HTTP via
  `httpx.AsyncClient`. Matches the Rust `reqwest` semantics.
- `FederatedMemoryHost` — fans out across a set of hosts and merges with
  Reciprocal Rank Fusion.
- `ReadOnlyMemoryHost` — wraps any host and refuses `upsert` / `delete`;
  used by subagents that inherit the parent's memory.

## Public API

```python
from corlinman_memory_host import (
    MemoryHost,            # Protocol
    MemoryQuery,           # query input
    MemoryHit,             # result row
    MemoryDoc,             # upsert input
    MemoryFilter,          # tag_eq / tag_in / created_after (sealed)
    HealthStatus,          # Ok / Degraded / Down
    LocalSqliteHost,
    RemoteHttpHost,
    FederatedMemoryHost,
    FusionStrategy,
    ReadOnlyMemoryHost,
    READ_ONLY_REJECT_TAG,
)
```

All `MemoryHost` methods are `async`. Errors are surfaced as
`MemoryHostError` (or stdlib exceptions for transport-level failure
when chained via `__cause__`).
