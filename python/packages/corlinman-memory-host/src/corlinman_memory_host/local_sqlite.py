"""Adapter that wraps an aiosqlite-backed FTS5 store behind the
:class:`MemoryHost` protocol.

Python port of ``rust/crates/corlinman-memory-host/src/local_sqlite.rs``.
The Rust impl reuses ``corlinman_vector::SqliteStore``; Python has no
equivalent published crate yet, so this module ships a self-contained
``_SqliteStore`` helper covering exactly the operations the Rust
``LocalSqliteHost`` consumes:

- ``files`` + ``chunks`` tables with the same column shape as
  ``corlinman-vector``'s ``SCHEMA_SQL`` (only the columns this adapter
  reads / writes — TFIDF, decay, tenant_id and friends are omitted as
  they're irrelevant to the memory-host surface).
- ``chunks_fts`` virtual table (FTS5, BM25) with INSERT/UPDATE/DELETE
  triggers, used by :meth:`_SqliteStore.search_bm25_with_filter` —
  matches the Rust ``search_bm25`` path the host adapter delegates to.
- ``memory_host_docs`` table — the exact ``CREATE TABLE`` script from
  ``ensure_memory_host_metadata_schema`` in ``corlinman-vector``.

Behaviour parity with the Rust adapter:

- ``query`` runs BM25, then expands one hop through ``links`` /
  back-links recorded in ``memory_host_docs.metadata``; expanded hits
  inherit ``seed_floor = max(seed_score) * 0.85`` and are tagged
  ``graph_expanded = True``.
- Seed hits with the same ``node_id`` collapse; host metadata wins over
  upserted metadata for the ``namespace`` / ``graph_expanded`` keys.
- ``upsert`` inserts a synthetic ``files`` row at
  ``memory-host://{nanos}-{counter}`` and one ``chunks`` row; the chunk
  id (as ``str``) is returned and is what callers pass to ``delete``.
- ``delete`` removes the chunk only (the synthetic file row is left
  behind, identical to the Rust path: ``SqliteStore::delete_chunk_by_id``
  is chunk-scoped).
- ``get`` returns ``None`` for unknown or non-numeric ids, never raises.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from corlinman_memory_host.base import MemoryHost
from corlinman_memory_host.types import (
    MemoryDoc,
    MemoryHit,
    MemoryHostError,
    MemoryQuery,
)

# Default diary-name tag recorded on synthetic ``files`` rows created by
# :meth:`LocalSqliteHost.upsert`. Kept stable so downstream tools can
# filter by it if they need to audit memory-host-originated content.
_DEFAULT_DIARY_NAME = "memory-host"


# ---------------------------------------------------------------------------
# Self-contained SQLite store (minimal surface)
# ---------------------------------------------------------------------------


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    diary_name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    mtime INTEGER NOT NULL,
    size INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    vector BLOB,
    namespace TEXT NOT NULL DEFAULT 'general',
    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_files_diary ON files(diary_name);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
CREATE INDEX IF NOT EXISTS idx_chunks_namespace ON chunks(namespace, id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content,
    content='chunks',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

_MEMORY_HOST_DOCS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_host_docs (
    chunk_id INTEGER PRIMARY KEY,
    namespace TEXT NOT NULL,
    metadata TEXT NOT NULL,
    node_id TEXT,
    FOREIGN KEY(chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_memory_host_docs_namespace_node
    ON memory_host_docs(namespace, node_id);
"""


class _ChunkRow:
    """Mirror of ``corlinman_vector::ChunkRow`` for the fields this adapter
    reads. Plain object (not a dataclass) so we can attach a couple of
    optional decoded values without ceremony."""

    __slots__ = ("chunk_index", "content", "file_id", "id", "namespace")

    def __init__(
        self,
        *,
        id: int,  # noqa: A002 — mirrors the SQL ``chunks.id`` column name
        file_id: int,
        chunk_index: int,
        content: str,
        namespace: str,
    ) -> None:
        self.id = id
        self.file_id = file_id
        self.chunk_index = chunk_index
        self.content = content
        self.namespace = namespace


class _MetaRow:
    __slots__ = ("chunk_id", "metadata", "namespace", "node_id")

    def __init__(
        self,
        *,
        chunk_id: int,
        namespace: str,
        metadata: str,
        node_id: str | None,
    ) -> None:
        self.chunk_id = chunk_id
        self.namespace = namespace
        self.metadata = metadata
        self.node_id = node_id


class _SqliteStore:
    """Self-contained aiosqlite-backed store for the memory-host adapter.

    Single connection guarded by an :class:`asyncio.Lock`; multi-host
    concurrency is unlikely in practice (one host per logical database)
    and the lock keeps the BM25 + metadata writes consistent.

    Use :meth:`open` to construct — the schema is created lazily on the
    first open so a fresh path is a no-config "just works" case (matches
    the Rust ``SqliteStore::open``).
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()
        self._memory_host_schema_ready = False

    @classmethod
    async def open(cls, path: str | Path) -> _SqliteStore:
        # ``:memory:`` is handled by aiosqlite the same as the on-disk
        # case; tests use a tmp dir so the schema/FTS5 triggers survive
        # repeat process invocations of the same DB.
        conn = await aiosqlite.connect(str(path))
        conn.row_factory = aiosqlite.Row
        # Foreign keys must be opted into per-connection in sqlite.
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.executescript(_SCHEMA_SQL)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    # ---- files / chunks ----------------------------------------------------

    async def insert_file(
        self,
        path: str,
        diary_name: str,
        checksum: str,
        mtime: int,
        size: int,
    ) -> int:
        async with self._lock:
            cur = await self._conn.execute(
                "INSERT INTO files(path, diary_name, checksum, mtime, size) "
                "VALUES (?, ?, ?, ?, ?)",
                (path, diary_name, checksum, mtime, size),
            )
            await self._conn.commit()
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    async def insert_chunk(
        self,
        file_id: int,
        chunk_index: int,
        content: str,
        vector: bytes | None,
        namespace: str,
    ) -> int:
        async with self._lock:
            cur = await self._conn.execute(
                "INSERT INTO chunks(file_id, chunk_index, content, vector, namespace) "
                "VALUES (?, ?, ?, ?, ?)",
                (file_id, chunk_index, content, vector, namespace),
            )
            await self._conn.commit()
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    async def delete_chunk_by_id(self, chunk_id: int) -> None:
        async with self._lock:
            await self._conn.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))
            await self._conn.commit()

    async def query_chunks_by_ids(self, ids: list[int]) -> list[_ChunkRow]:
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        sql = (
            "SELECT id, file_id, chunk_index, content, namespace "
            f"FROM chunks WHERE id IN ({placeholders})"
        )
        async with self._conn.execute(sql, ids) as cur:
            rows = await cur.fetchall()
        return [
            _ChunkRow(
                id=int(r["id"]),
                file_id=int(r["file_id"]),
                chunk_index=int(r["chunk_index"]),
                content=str(r["content"]),
                namespace=str(r["namespace"]),
            )
            for r in rows
        ]

    async def filter_chunk_ids_by_namespace(self, namespaces: list[str]) -> list[int]:
        if not namespaces:
            return []
        placeholders = ",".join("?" * len(namespaces))
        sql = (
            "SELECT id FROM chunks "
            f"WHERE namespace IN ({placeholders}) ORDER BY id ASC"
        )
        async with self._conn.execute(sql, namespaces) as cur:
            rows = await cur.fetchall()
        return [int(r["id"]) for r in rows]

    async def search_bm25_with_filter(
        self,
        text: str,
        top_k: int,
        allowed_ids: list[int] | None,
    ) -> list[tuple[int, float]]:
        """Run an FTS5 BM25 search; optionally restrict to ``allowed_ids``.

        Returns ``(chunk_id, score)`` pairs in descending score order.
        FTS5's ``bm25()`` is "lower = more relevant" (it's a negative
        log score); we negate it so the adapter's ``score`` matches the
        Rust "higher = better" contract."""
        if top_k <= 0:
            return []
        # FTS5 MATCH syntax: bare phrase. To stay forgiving on Latin-1
        # text we don't quote-escape; if a future caller needs phrase
        # search they can pre-quote.
        if allowed_ids is None:
            sql = (
                "SELECT rowid, bm25(chunks_fts) AS score "
                "FROM chunks_fts WHERE chunks_fts MATCH ? "
                "ORDER BY score ASC LIMIT ?"
            )
            params: tuple[Any, ...] = (text, top_k)
        else:
            if not allowed_ids:
                return []
            placeholders = ",".join("?" * len(allowed_ids))
            sql = (
                "SELECT rowid, bm25(chunks_fts) AS score "
                "FROM chunks_fts WHERE chunks_fts MATCH ? "
                f"AND rowid IN ({placeholders}) "
                "ORDER BY score ASC LIMIT ?"
            )
            params = (text, *allowed_ids, top_k)
        try:
            async with self._conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
        except aiosqlite.OperationalError as exc:
            # FTS5 raises on malformed queries (stray operators, all
            # stop-words). Mirror the Rust path: an "empty" result is
            # the right answer for a query that doesn't tokenise.
            if "fts5" in str(exc).lower() or "malformed" in str(exc).lower():
                return []
            raise
        # bm25() returns negative floats; flip so larger = better, matching
        # the Rust SqliteStore::search_bm25 contract (its row.score is
        # already the higher-is-better orientation).
        return [(int(r["rowid"]), -float(r["score"])) for r in rows]

    # ---- memory_host_docs --------------------------------------------------

    async def ensure_memory_host_metadata_schema(self) -> None:
        if self._memory_host_schema_ready:
            return
        async with self._lock:
            if self._memory_host_schema_ready:
                return
            await self._conn.executescript(_MEMORY_HOST_DOCS_SCHEMA_SQL)
            await self._conn.commit()
            self._memory_host_schema_ready = True

    async def upsert_memory_host_metadata(
        self,
        chunk_id: int,
        namespace: str,
        metadata: str,
        node_id: str | None,
    ) -> None:
        async with self._lock:
            await self._conn.execute(
                "INSERT INTO memory_host_docs(chunk_id, namespace, metadata, node_id) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chunk_id) DO UPDATE SET "
                "  namespace = excluded.namespace, "
                "  metadata = excluded.metadata, "
                "  node_id = excluded.node_id",
                (chunk_id, namespace, metadata, node_id),
            )
            await self._conn.commit()

    async def memory_host_metadata_by_chunk_ids(
        self, chunk_ids: list[int]
    ) -> list[_MetaRow]:
        if not chunk_ids:
            return []
        placeholders = ",".join("?" * len(chunk_ids))
        sql = (
            "SELECT chunk_id, namespace, metadata, node_id "
            f"FROM memory_host_docs WHERE chunk_id IN ({placeholders})"
        )
        async with self._conn.execute(sql, chunk_ids) as cur:
            rows = await cur.fetchall()
        return [
            _MetaRow(
                chunk_id=int(r["chunk_id"]),
                namespace=str(r["namespace"]),
                metadata=str(r["metadata"]),
                node_id=str(r["node_id"]) if r["node_id"] is not None else None,
            )
            for r in rows
        ]

    async def memory_host_chunk_ids_by_node_ids(
        self, node_ids: list[str], namespace: str | None
    ) -> list[int]:
        if not node_ids:
            return []
        placeholders = ",".join("?" * len(node_ids))
        sql = f"SELECT chunk_id FROM memory_host_docs WHERE node_id IN ({placeholders})"
        params: list[Any] = list(node_ids)
        if namespace is not None:
            sql += " AND namespace = ?"
            params.append(namespace)
        sql += " ORDER BY chunk_id ASC"
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [int(r["chunk_id"]) for r in rows]

    async def list_memory_host_metadata(
        self, namespace: str | None
    ) -> list[_MetaRow]:
        sql = "SELECT chunk_id, namespace, metadata, node_id FROM memory_host_docs"
        params: list[Any] = []
        if namespace is not None:
            sql += " WHERE namespace = ?"
            params.append(namespace)
        sql += " ORDER BY chunk_id ASC"
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [
            _MetaRow(
                chunk_id=int(r["chunk_id"]),
                namespace=str(r["namespace"]),
                metadata=str(r["metadata"]),
                node_id=str(r["node_id"]) if r["node_id"] is not None else None,
            )
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Local helpers — straight ports of free functions in local_sqlite.rs
# ---------------------------------------------------------------------------


def _merge_metadata(base: dict[str, Any], stored: Any | None) -> dict[str, Any]:
    """Merge host-derived ``base`` into upserted ``stored`` metadata.

    Host keys win on collision (mirrors the Rust ``merge_metadata``: the
    base ``Value::Object`` overwrites the stored object). Non-object
    stored metadata is dropped — exactly the Rust behaviour, which only
    merges when the stored value is ``Value::Object``."""
    out: dict[str, Any] = {}
    if isinstance(stored, dict):
        out.update(stored)
    out.update(base)
    return out


def _json_string_array(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def _dedupe_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# LocalSqliteHost
# ---------------------------------------------------------------------------


class LocalSqliteHost(MemoryHost):
    """:class:`MemoryHost` adapter over a local SQLite + FTS5 database.

    Construction is **not** ``__init__``-only: use :meth:`open` so the
    underlying ``aiosqlite`` schema bootstrap is awaited cleanly. The
    Rust ``LocalSqliteHost::new`` takes an already-opened ``SqliteStore``;
    the Python equivalent is :meth:`with_store` if you want to share a
    store across multiple hosts.
    """

    def __init__(self, host_name: str, store: _SqliteStore) -> None:
        self._name = host_name
        self._store = store
        # Monotonic counter appended to synthetic file paths so repeated
        # upserts within the same nanosecond don't collide on the
        # ``files.path UNIQUE`` constraint. Lock-free is fine — single
        # event loop owns it.
        self._upsert_counter = 0

    # ---- construction -----------------------------------------------------

    @classmethod
    async def open(
        cls, host_name: str, path: str | Path
    ) -> LocalSqliteHost:
        """Open (or create) the SQLite DB at ``path`` and wrap it."""
        store = await _SqliteStore.open(path)
        return cls(host_name, store)

    @classmethod
    def with_store(cls, host_name: str, store: _SqliteStore) -> LocalSqliteHost:
        """Wrap an already-opened :class:`_SqliteStore`. Used when one
        store backs several adapters (e.g. a read-only sibling)."""
        return cls(host_name, store)

    @property
    def store(self) -> _SqliteStore:
        """Borrow the underlying store (primarily for tests)."""
        return self._store

    async def close(self) -> None:
        """Close the owned aiosqlite connection. Idempotent."""
        await self._store.close()

    # ---- MemoryHost surface -----------------------------------------------

    def name(self) -> str:
        return self._name

    async def query(self, req: MemoryQuery) -> list[MemoryHit]:
        # Structured filters are ignored in the skeleton — mirror the
        # Rust adapter's ``debug!`` log line. We do not raise.
        if req.top_k == 0 or not req.text.strip():
            return []

        # Namespace pushdown: same path as the Rust adapter — collect
        # the chunk id whitelist before running BM25.
        allowed_ids: list[int] | None = None
        if req.namespace is not None:
            try:
                allowed_ids = await self._store.filter_chunk_ids_by_namespace(
                    [req.namespace]
                )
            except aiosqlite.Error as exc:
                raise MemoryHostError(
                    f"LocalSqliteHost: namespace filter: {exc}"
                ) from exc

        await self._ensure_metadata_schema()

        try:
            hits = await self._store.search_bm25_with_filter(
                req.text, req.top_k, allowed_ids
            )
        except aiosqlite.Error as exc:
            raise MemoryHostError(f"LocalSqliteHost: BM25 search: {exc}") from exc

        if not hits:
            return []

        # ``scored`` carries (chunk_id, score, graph_expanded). Seed
        # hits keep their BM25 score; graph-expanded hits get
        # ``seed_floor = max(seed) * 0.85`` so they rank below all seeds.
        scored: list[tuple[int, float, bool]] = [
            (cid, score, False) for (cid, score) in hits
        ]
        seed_ids = [cid for (cid, _) in hits]
        expanded_ids = await self._one_hop_graph_ids(seed_ids, req.namespace)
        seen_ids: set[int] = set(seed_ids)
        seed_floor = max(score for (_, score) in hits) * 0.85
        for cid in expanded_ids:
            if cid not in seen_ids:
                seen_ids.add(cid)
                scored.append((cid, seed_floor, True))

        candidate_ids = [cid for (cid, _, _) in scored]
        metadata_by_id = await self._metadata_for_chunk_ids(candidate_ids)

        # Dedupe by ``node_id`` and apply the ``top_k`` budget BEFORE
        # hydrating chunks. First seen ``node_id`` wins — matches the
        # Rust order-preserving HashSet logic.
        budgeted: list[tuple[int, float, bool]] = []
        seen_node_ids: set[str] = set()
        for cid, score, graph_expanded in scored:
            stored = metadata_by_id.get(cid)
            if isinstance(stored, dict):
                node_id = stored.get("node_id")
                if isinstance(node_id, str):
                    if node_id in seen_node_ids:
                        continue
                    seen_node_ids.add(node_id)
            budgeted.append((cid, score, graph_expanded))
            if len(budgeted) >= req.top_k:
                break

        ids = [cid for (cid, _, _) in budgeted]
        try:
            chunks = await self._store.query_chunks_by_ids(ids)
        except aiosqlite.Error as exc:
            raise MemoryHostError(
                f"LocalSqliteHost: hydrate chunks: {exc}"
            ) from exc

        by_id: dict[int, _ChunkRow] = {c.id: c for c in chunks}

        out: list[MemoryHit] = []
        for cid, score, graph_expanded in budgeted:
            c = by_id.get(cid)
            if c is None:
                continue
            host_base = {
                "file_id": c.file_id,
                "chunk_index": c.chunk_index,
                "namespace": c.namespace,
                "graph_expanded": graph_expanded,
            }
            metadata = _merge_metadata(host_base, metadata_by_id.get(cid))
            out.append(
                MemoryHit(
                    id=str(cid),
                    content=c.content,
                    score=score,
                    source=self._name,
                    metadata=metadata,
                )
            )
        return out

    async def upsert(self, doc: MemoryDoc) -> str:
        counter = self._upsert_counter
        self._upsert_counter += 1
        nanos = time.time_ns()
        synthetic_path = f"memory-host://{nanos}-{counter}"

        try:
            file_id = await self._store.insert_file(
                synthetic_path, _DEFAULT_DIARY_NAME, "", 0, 0
            )
        except aiosqlite.Error as exc:
            raise MemoryHostError(
                f"LocalSqliteHost: insert synthetic file row: {exc}"
            ) from exc

        namespace = doc.namespace if doc.namespace is not None else "general"
        try:
            chunk_id = await self._store.insert_chunk(
                file_id, 0, doc.content, None, namespace
            )
        except aiosqlite.Error as exc:
            raise MemoryHostError(
                f"LocalSqliteHost: insert chunk: {exc}"
            ) from exc

        await self._ensure_metadata_schema()
        await self._upsert_metadata(chunk_id, namespace, doc.metadata)
        return str(chunk_id)

    async def delete(self, doc_id: str) -> None:
        try:
            chunk_id = int(doc_id)
        except ValueError as exc:
            raise MemoryHostError(
                f"LocalSqliteHost: invalid chunk id '{doc_id}'"
            ) from exc
        try:
            await self._store.delete_chunk_by_id(chunk_id)
        except aiosqlite.Error as exc:
            raise MemoryHostError(
                f"LocalSqliteHost: delete chunk: {exc}"
            ) from exc

    async def get(self, doc_id: str) -> MemoryHit | None:
        # Phase 4 W3 C1 (MCP ``resources/read`` over
        # ``corlinman://memory/``) — single-row lookup keyed by the id
        # returned from ``upsert`` / ``query``.
        try:
            chunk_id = int(doc_id)
        except ValueError:
            return None
        try:
            rows = await self._store.query_chunks_by_ids([chunk_id])
        except aiosqlite.Error as exc:
            raise MemoryHostError(
                f"LocalSqliteHost.get: query chunk by id: {exc}"
            ) from exc
        if not rows:
            return None
        chunk = rows[0]
        await self._ensure_metadata_schema()
        metadata_by_id = await self._metadata_for_chunk_ids([chunk_id])
        host_base = {
            "file_id": chunk.file_id,
            "chunk_index": chunk.chunk_index,
            "namespace": chunk.namespace,
        }
        metadata = _merge_metadata(host_base, metadata_by_id.get(chunk_id))
        return MemoryHit(
            id=str(chunk.id),
            content=chunk.content,
            # Direct-lookup sentinel — no relevance score (caller didn't
            # pose a query). 1.0 = "fully matched".
            score=1.0,
            source=self._name,
            metadata=metadata,
        )

    # ---- internal helpers -------------------------------------------------

    async def _ensure_metadata_schema(self) -> None:
        try:
            await self._store.ensure_memory_host_metadata_schema()
        except aiosqlite.Error as exc:
            raise MemoryHostError(
                f"LocalSqliteHost: ensure metadata schema: {exc}"
            ) from exc

    async def _upsert_metadata(
        self,
        chunk_id: int,
        namespace: str,
        metadata: Any,
    ) -> None:
        # ``node_id`` is hoisted out so the indexed column can be used by
        # ``memory_host_chunk_ids_by_node_ids`` without a full scan.
        node_id: str | None = None
        if isinstance(metadata, dict):
            n = metadata.get("node_id")
            if isinstance(n, str):
                node_id = n
        # The Rust impl serialises ``metadata: &Value`` via ``.to_string()``
        # which uses the default ``serde_json`` formatter. ``json.dumps``
        # without ``ensure_ascii=False`` matches that closely enough for
        # round-tripping — the column is opaque to the schema.
        metadata_json = json.dumps(metadata if metadata is not None else None)
        try:
            await self._store.upsert_memory_host_metadata(
                chunk_id, namespace, metadata_json, node_id
            )
        except aiosqlite.Error as exc:
            raise MemoryHostError(
                f"LocalSqliteHost: upsert metadata: {exc}"
            ) from exc

    async def _metadata_for_chunk_ids(
        self, chunk_ids: list[int]
    ) -> dict[int, Any]:
        if not chunk_ids:
            return {}
        try:
            rows = await self._store.memory_host_metadata_by_chunk_ids(chunk_ids)
        except aiosqlite.Error as exc:
            raise MemoryHostError(
                f"LocalSqliteHost: query metadata by chunk ids: {exc}"
            ) from exc
        out: dict[int, Any] = {}
        for row in rows:
            try:
                value = json.loads(row.metadata) if row.metadata else None
            except json.JSONDecodeError:
                value = None
            out[row.chunk_id] = value
        return out

    async def _one_hop_graph_ids(
        self,
        seed_chunk_ids: list[int],
        namespace: str | None,
    ) -> list[int]:
        if not seed_chunk_ids:
            return []
        seed_metadata = await self._metadata_for_chunk_ids(seed_chunk_ids)
        seed_node_ids: list[str] = []
        linked_node_ids: list[str] = []
        for metadata in seed_metadata.values():
            if isinstance(metadata, dict):
                nid = metadata.get("node_id")
                if isinstance(nid, str):
                    seed_node_ids.append(nid)
                linked_node_ids.extend(_json_string_array(metadata.get("links")))
        wanted: list[str] = []
        wanted.extend(linked_node_ids)
        try:
            wanted.extend(
                await self._backlinked_node_ids(seed_node_ids, namespace)
            )
        except aiosqlite.Error as exc:
            raise MemoryHostError(
                f"LocalSqliteHost: query backlinks: {exc}"
            ) from exc
        wanted = _dedupe_strings(wanted)
        if not wanted:
            return []
        return await self._chunk_ids_for_node_ids(wanted, namespace)

    async def _backlinked_node_ids(
        self,
        seed_node_ids: list[str],
        namespace: str | None,
    ) -> list[str]:
        if not seed_node_ids:
            return []
        try:
            rows = await self._store.list_memory_host_metadata(namespace)
        except aiosqlite.Error as exc:
            raise MemoryHostError(
                f"LocalSqliteHost: scan graph metadata: {exc}"
            ) from exc
        seed_set = set(seed_node_ids)
        out: list[str] = []
        for row in rows:
            try:
                metadata = json.loads(row.metadata) if row.metadata else None
            except json.JSONDecodeError:
                metadata = None
            links = _json_string_array(
                metadata.get("links") if isinstance(metadata, dict) else None
            )
            if any(link in seed_set for link in links) and row.node_id is not None:
                out.append(row.node_id)
        return _dedupe_strings(out)

    async def _chunk_ids_for_node_ids(
        self, node_ids: list[str], namespace: str | None
    ) -> list[int]:
        if not node_ids:
            return []
        try:
            return await self._store.memory_host_chunk_ids_by_node_ids(
                node_ids, namespace
            )
        except aiosqlite.Error as exc:
            raise MemoryHostError(
                f"LocalSqliteHost: query one-hop chunks: {exc}"
            ) from exc


__all__ = ["LocalSqliteHost"]
