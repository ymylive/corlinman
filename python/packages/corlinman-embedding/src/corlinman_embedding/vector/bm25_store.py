"""aiosqlite pool + file/chunk/kv/tag/EPA access + BM25 FTS5 search.

Python port of the RAG-relevant subset of :mod:`corlinman_vector::sqlite`.

Tables (schema v6, identical to the Rust crate):

- ``files`` — one row per indexed source file.
- ``chunks`` — text chunks + little-endian f32 BLOB vector + namespace tag.
- ``chunks_fts`` — FTS5 contentless-linked virtual table mirroring
  ``chunks.content``, maintained by INSERT/DELETE/UPDATE triggers.
- ``tag_nodes`` — hierarchical tag tree (dotted paths).
- ``chunk_tags`` — (chunk_id, tag_node_id) many-to-many.
- ``chunk_epa`` — per-chunk EPA cache (projections / entropy / logic_depth).
- ``kv_store`` — general KV cache + ``schema_version``.

The BM25 path uses SQLite's built-in ``bm25()`` ranker (FTS5 ships with
the standard ``sqlite3`` extension that aiosqlite delegates to).

Out of scope for the Python port (Rust-side only): ``pending_approvals``,
``memory_host_docs`` helpers, schema migration registry, tenant_id ALTER
shims, decay helpers (``apply_decay_to_scored`` / ``record_recall`` /
``promote_to_consolidated`` etc.). These are not part of the
hybrid-retrieval contract and ship with the Rust crate as the source of
truth — the Python side only needs read/write into the same schema for
the RAG path.
"""

from __future__ import annotations

import asyncio
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import aiosqlite

__all__ = [
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "ChunkRow",
    "ChunkEpaRow",
    "FileRow",
    "TagNodeRow",
    "SqliteStore",
    "f32_slice_to_blob",
    "blob_to_f32_vec",
]


#: Current corlinman schema version written to ``kv_store('schema_version')``.
#: Kept in lockstep with Rust's ``corlinman_vector::SCHEMA_VERSION``.
SCHEMA_VERSION: int = 6


#: Full CREATE TABLE + CREATE INDEX script used when opening a fresh DB.
#: All statements are ``IF NOT EXISTS`` so this is safe to re-run.
#: Byte-aligned with Rust's ``SCHEMA_SQL`` (modulo whitespace) so a DB
#: created by either side is readable by the other.
SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    diary_name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    mtime INTEGER NOT NULL,
    size INTEGER NOT NULL,
    updated_at INTEGER,
    tenant_id TEXT NOT NULL DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    vector BLOB,
    namespace TEXT NOT NULL DEFAULT 'general',
    decay_score REAL NOT NULL DEFAULT 1.0,
    consolidated_at INTEGER,
    last_recalled_at INTEGER,
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    tenant_id TEXT NOT NULL DEFAULT 'default',
    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS kv_store (
    key TEXT PRIMARY KEY,
    value TEXT,
    vector BLOB,
    tenant_id TEXT NOT NULL DEFAULT 'default'
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

CREATE TABLE IF NOT EXISTS tag_nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id   INTEGER REFERENCES tag_nodes(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    path        TEXT NOT NULL UNIQUE,
    depth       INTEGER NOT NULL,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    tenant_id   TEXT NOT NULL DEFAULT 'default'
);

CREATE INDEX IF NOT EXISTS idx_tag_nodes_parent ON tag_nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_tag_nodes_path   ON tag_nodes(path);
CREATE INDEX IF NOT EXISTS idx_tag_nodes_depth  ON tag_nodes(depth);

CREATE TABLE IF NOT EXISTS chunk_tags (
    chunk_id     INTEGER NOT NULL,
    tag_node_id  INTEGER NOT NULL,
    PRIMARY KEY (chunk_id, tag_node_id),
    FOREIGN KEY (chunk_id)    REFERENCES chunks(id)    ON DELETE CASCADE,
    FOREIGN KEY (tag_node_id) REFERENCES tag_nodes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunk_tags_tag_node ON chunk_tags(tag_node_id);

CREATE TABLE IF NOT EXISTS chunk_epa (
    chunk_id     INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    projections  BLOB    NOT NULL,
    entropy      REAL    NOT NULL,
    logic_depth  REAL    NOT NULL,
    computed_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
"""


# ---------------------------------------------------------------------------
# Row dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileRow:
    id: int
    path: str
    diary_name: str
    checksum: str
    mtime: int
    size: int
    updated_at: int | None


@dataclass(frozen=True)
class ChunkRow:
    id: int
    file_id: int
    chunk_index: int
    content: str
    #: Decoded vector (little-endian f32). ``None`` if the BLOB is NULL or
    #: the length wasn't a multiple of 4.
    vector: list[float] | None
    namespace: str


@dataclass(frozen=True)
class TagNodeRow:
    id: int
    parent_id: int | None
    name: str
    path: str
    depth: int


@dataclass(frozen=True)
class ChunkEpaRow:
    chunk_id: int
    projections: list[float]
    entropy: float
    logic_depth: float
    computed_at: int


# ---------------------------------------------------------------------------
# BLOB <-> f32 helpers (mirrors lib.rs)
# ---------------------------------------------------------------------------


def f32_slice_to_blob(values: Sequence[float]) -> bytes:
    """Encode a sequence of floats as a little-endian f32 byte BLOB."""

    return struct.pack(f"<{len(values)}f", *values)


def blob_to_f32_vec(blob: bytes | memoryview | None) -> list[float] | None:
    """Decode a little-endian f32 BLOB. Returns ``None`` on length mismatch."""

    if blob is None:
        return None
    raw = bytes(blob)
    if len(raw) % 4 != 0:
        return None
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


# ---------------------------------------------------------------------------
# SqliteStore — async wrapper over a single shared aiosqlite connection.
# ---------------------------------------------------------------------------


def _now_unix_s() -> int:
    import time

    return int(time.time())


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


class SqliteStore:
    """Async wrapper over an aiosqlite connection pointed at ``knowledge_base.sqlite``.

    Opens the file with WAL + ``foreign_keys=ON`` and runs :data:`SCHEMA_SQL`
    unconditionally (``CREATE … IF NOT EXISTS``).

    aiosqlite serialises every statement through its background thread, so
    we hold a single connection per :class:`SqliteStore` instead of a pool
    — the contention model is identical to sqlx's ``max_connections=1``
    setup and avoids surprises around WAL/shm under high concurrency.
    """

    __slots__ = ("_conn", "_path", "_lock")

    def __init__(self, conn: aiosqlite.Connection, path: Path) -> None:
        self._conn = conn
        self._path = path
        # Serialise writes — aiosqlite's connection is already single-thread
        # but interleaving partial transactions across coroutines breaks
        # FTS5 triggers in subtle ways.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Construction / teardown
    # ------------------------------------------------------------------

    @classmethod
    async def open(cls, path: str | os.PathLike[str]) -> "SqliteStore":
        """Open (or create) a SQLite file at ``path`` with the v6 schema applied."""

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(p))
        # PRAGMAs first — order matches the Rust SqliteConnectOptions chain.
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        # Truncate WAL into the main DB so subsequent reopens see schema
        # without a WAL replay (matches Rust's checkpoint-after-migrate).
        await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        await conn.commit()
        return cls(conn, p)

    async def close(self) -> None:
        """Close the underlying connection. Idempotent."""

        try:
            await self._conn.close()
        except Exception:  # pragma: no cover - defensive
            pass

    @property
    def path(self) -> Path:
        return self._path

    @property
    def connection(self) -> aiosqlite.Connection:
        """Borrow the underlying aiosqlite connection (mostly for tests)."""

        return self._conn

    # ------------------------------------------------------------------
    # files
    # ------------------------------------------------------------------

    async def insert_file(
        self,
        path: str,
        diary_name: str,
        checksum: str,
        mtime: int,
        size: int,
    ) -> int:
        """Insert a row into ``files``; returns ``lastInsertRowid``."""

        async with self._lock:
            cursor = await self._conn.execute(
                "INSERT INTO files(path, diary_name, checksum, mtime, size, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (path, diary_name, checksum, int(mtime), int(size), _now_unix_s()),
            )
            await self._conn.commit()
            row_id = cursor.lastrowid
            await cursor.close()
            assert row_id is not None
            return int(row_id)

    async def list_files(self) -> list[FileRow]:
        """List every row in ``files`` ordered by ``id ASC``."""

        async with self._conn.execute(
            "SELECT id, path, diary_name, checksum, mtime, size, updated_at "
            "FROM files ORDER BY id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            FileRow(
                id=int(r[0]),
                path=str(r[1]),
                diary_name=str(r[2]),
                checksum=str(r[3]),
                mtime=int(r[4]),
                size=int(r[5]),
                updated_at=int(r[6]) if r[6] is not None else None,
            )
            for r in rows
        ]

    async def count_files(self) -> int:
        async with self._conn.execute("SELECT COUNT(*) FROM files") as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # chunks
    # ------------------------------------------------------------------

    async def insert_chunk(
        self,
        file_id: int,
        chunk_index: int,
        content: str,
        vector: Sequence[float] | None,
        namespace: str = "general",
    ) -> int:
        """Insert a chunk; returns its auto-assigned ``id``."""

        blob = f32_slice_to_blob(vector) if vector is not None else None
        async with self._lock:
            cursor = await self._conn.execute(
                "INSERT INTO chunks(file_id, chunk_index, content, vector, namespace) "
                "VALUES (?, ?, ?, ?, ?)",
                (int(file_id), int(chunk_index), content, blob, namespace),
            )
            await self._conn.commit()
            row_id = cursor.lastrowid
            await cursor.close()
            assert row_id is not None
            return int(row_id)

    async def get_chunks(self, file_id: int) -> list[ChunkRow]:
        """Chunks belonging to ``file_id``, ordered by ``chunk_index``."""

        async with self._conn.execute(
            "SELECT id, file_id, chunk_index, content, vector, namespace "
            "FROM chunks WHERE file_id = ? ORDER BY chunk_index ASC",
            (int(file_id),),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_chunk(r) for r in rows]

    async def query_chunks_by_ids(self, ids: Sequence[int]) -> list[ChunkRow]:
        """Fetch chunks by id list; preserves caller-supplied order."""

        if not ids:
            return []
        sql = (
            f"SELECT id, file_id, chunk_index, content, vector, namespace "
            f"FROM chunks WHERE id IN ({_placeholders(len(ids))})"
        )
        async with self._conn.execute(sql, tuple(int(i) for i in ids)) as cursor:
            rows = await cursor.fetchall()
        chunks = [_row_to_chunk(r) for r in rows]
        order = {int(cid): i for i, cid in enumerate(ids)}
        chunks.sort(key=lambda c: order.get(c.id, 10**9))
        return chunks

    async def count_chunks(self) -> int:
        async with self._conn.execute("SELECT COUNT(*) FROM chunks") as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def delete_chunk_by_id(self, chunk_id: int) -> int:
        """Delete a single chunk by id. Returns the rows-affected count."""

        async with self._lock:
            cursor = await self._conn.execute(
                "DELETE FROM chunks WHERE id = ?", (int(chunk_id),)
            )
            await self._conn.commit()
            n = cursor.rowcount
            await cursor.close()
            return int(n) if n is not None else 0

    # ------------------------------------------------------------------
    # BM25 / FTS5
    # ------------------------------------------------------------------

    async def search_bm25(self, query: str, limit: int) -> list[tuple[int, float]]:
        """BM25 full-text search over ``chunks.content``.

        Returns ``(chunk_id, score)`` ordered best-first. FTS5's ``bm25()``
        returns a non-positive number (smaller = more relevant); we negate
        it so callers see a positive, larger-is-better score consistent
        with the rest of the API.
        """

        if not query.strip() or limit <= 0:
            return []
        async with self._conn.execute(
            "SELECT rowid AS id, bm25(chunks_fts) AS score "
            "FROM chunks_fts WHERE chunks_fts MATCH ? "
            "ORDER BY score ASC LIMIT ?",
            (query, int(limit)),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(int(r[0]), float(-r[1])) for r in rows]

    async def search_bm25_with_filter(
        self,
        query: str,
        limit: int,
        allowed_ids: Sequence[int] | None,
    ) -> list[tuple[int, float]]:
        """BM25 search restricted to ``allowed_ids``.

        - ``allowed_ids is None`` ⇒ identical to :meth:`search_bm25`.
        - Empty sequence ⇒ returns no hits without hitting SQLite.
        """

        if not query.strip() or limit <= 0:
            return []
        if allowed_ids is None:
            return await self.search_bm25(query, limit)
        ids = list(allowed_ids)
        if not ids:
            return []
        sql = (
            f"SELECT rowid AS id, bm25(chunks_fts) AS score "
            f"FROM chunks_fts WHERE chunks_fts MATCH ? "
            f"AND rowid IN ({_placeholders(len(ids))}) "
            f"ORDER BY score ASC LIMIT ?"
        )
        params: tuple = (query, *[int(i) for i in ids], int(limit))
        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [(int(r[0]), float(-r[1])) for r in rows]

    async def rebuild_fts(self) -> None:
        """Backfill ``chunks_fts`` from the existing ``chunks`` table."""

        async with self._lock:
            await self._conn.execute(
                "INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')"
            )
            await self._conn.commit()

    # ------------------------------------------------------------------
    # tag_nodes / chunk_tags (schema v6, hierarchical)
    # ------------------------------------------------------------------

    async def ensure_tag_path(self, path: str) -> int:
        """Upsert every segment of a dotted ``path``; return the leaf node id."""

        if not path or any(not seg.strip() for seg in path.split(".")):
            raise ValueError(f"ensure_tag_path: invalid path '{path}'")
        segments = path.split(".")
        parent_id: int | None = None
        cur_path = ""
        last_id = 0
        async with self._lock:
            for depth, seg in enumerate(segments):
                cur_path = seg if depth == 0 else f"{cur_path}.{seg}"
                async with self._conn.execute(
                    "SELECT id FROM tag_nodes WHERE path = ?", (cur_path,)
                ) as cursor:
                    row = await cursor.fetchone()
                if row is not None:
                    node_id = int(row[0])
                else:
                    cursor = await self._conn.execute(
                        "INSERT INTO tag_nodes(parent_id, name, path, depth) "
                        "VALUES (?, ?, ?, ?)",
                        (parent_id, seg, cur_path, int(depth)),
                    )
                    node_id = int(cursor.lastrowid or 0)
                    await cursor.close()
                parent_id = node_id
                last_id = node_id
            await self._conn.commit()
        return last_id

    async def insert_tag(self, chunk_id: int, tag: str) -> None:
        """Attach ``tag`` (a dotted path) to ``chunk_id``. Idempotent."""

        node_id = await self.ensure_tag_path(tag)
        async with self._lock:
            await self._conn.execute(
                "INSERT OR IGNORE INTO chunk_tags(chunk_id, tag_node_id) VALUES (?, ?)",
                (int(chunk_id), node_id),
            )
            await self._conn.commit()

    async def attach_chunk_to_tag_path(self, chunk_id: int, path: str) -> None:
        """Same as :meth:`insert_tag`; explicit alias matching the Rust API."""

        await self.insert_tag(chunk_id, path)

    async def get_tags(self, chunk_id: int) -> list[str]:
        """Tags attached to ``chunk_id`` as dotted paths, sorted ascending."""

        async with self._conn.execute(
            "SELECT tn.path FROM chunk_tags ct "
            "JOIN tag_nodes tn ON tn.id = ct.tag_node_id "
            "WHERE ct.chunk_id = ? ORDER BY tn.path ASC",
            (int(chunk_id),),
        ) as cursor:
            rows = await cursor.fetchall()
        return [str(r[0]) for r in rows]

    async def count_tags(self) -> int:
        """Distinct tag count across ``chunk_tags``."""

        async with self._conn.execute(
            "SELECT COUNT(DISTINCT tn.path) FROM chunk_tags ct "
            "JOIN tag_nodes tn ON tn.id = ct.tag_node_id"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def filter_chunk_ids_by_tag_subtree(self, path: str) -> list[int]:
        """Chunks tagged anywhere in the subtree rooted at ``path``."""

        like_pattern = f"{path}.%"
        async with self._conn.execute(
            "SELECT DISTINCT ct.chunk_id FROM chunk_tags ct "
            "JOIN tag_nodes tn ON tn.id = ct.tag_node_id "
            "WHERE tn.path = ? OR tn.path LIKE ? "
            "ORDER BY ct.chunk_id ASC",
            (path, like_pattern),
        ) as cursor:
            rows = await cursor.fetchall()
        return [int(r[0]) for r in rows]

    async def filter_chunk_ids_by_tags(
        self,
        required: Sequence[str] = (),
        any_of: Sequence[str] = (),
        excluded: Sequence[str] = (),
    ) -> list[int]:
        """Resolve a tag filter into a sorted whitelist of ``chunk.id``s.

        Semantics:

        - ``required``: chunk must carry **every** tag listed.
        - ``any_of``: chunk must carry **at least one** tag listed
          (ignored when empty).
        - ``excluded``: chunk must carry **none** of the listed tags.
        - All empty ⇒ returns every ``chunks.id`` (matching Rust).
        """

        req = list(required)
        any_ = list(any_of)
        exc = list(excluded)

        if not req and not any_ and not exc:
            async with self._conn.execute(
                "SELECT id FROM chunks ORDER BY id ASC"
            ) as cursor:
                rows = await cursor.fetchall()
            return [int(r[0]) for r in rows]

        sql_parts = ["SELECT DISTINCT c.id FROM chunks c"]
        where: list[str] = []
        binds: list[str] = []
        if req:
            sql_parts.append(
                " JOIN chunk_tags ct_req ON ct_req.chunk_id = c.id "
                " JOIN tag_nodes tn_req ON tn_req.id = ct_req.tag_node_id"
            )
            where.append(f"tn_req.path IN ({_placeholders(len(req))})")
            binds.extend(req)
        if any_:
            where.append(
                "EXISTS (SELECT 1 FROM chunk_tags ct_any "
                "JOIN tag_nodes tn_any ON tn_any.id = ct_any.tag_node_id "
                f"WHERE ct_any.chunk_id = c.id AND tn_any.path IN ({_placeholders(len(any_))}))"
            )
            binds.extend(any_)
        if exc:
            where.append(
                "NOT EXISTS (SELECT 1 FROM chunk_tags ct_exc "
                "JOIN tag_nodes tn_exc ON tn_exc.id = ct_exc.tag_node_id "
                f"WHERE ct_exc.chunk_id = c.id AND tn_exc.path IN ({_placeholders(len(exc))}))"
            )
            binds.extend(exc)
        if where:
            sql_parts.append(" WHERE " + " AND ".join(where))
        if req:
            sql_parts.append(
                f" GROUP BY c.id HAVING COUNT(DISTINCT tn_req.path) = {len(req)}"
            )
        sql_parts.append(" ORDER BY c.id ASC")
        sql = "".join(sql_parts)
        async with self._conn.execute(sql, tuple(binds)) as cursor:
            rows = await cursor.fetchall()
        return [int(r[0]) for r in rows]

    # ------------------------------------------------------------------
    # chunk_epa (schema v6)
    # ------------------------------------------------------------------

    async def upsert_chunk_epa(
        self,
        chunk_id: int,
        projections: Sequence[float],
        entropy: float,
        logic_depth: float,
    ) -> None:
        """Upsert a per-chunk EPA cache row. Refreshes ``computed_at``."""

        blob = f32_slice_to_blob(projections)
        async with self._lock:
            await self._conn.execute(
                "INSERT INTO chunk_epa(chunk_id, projections, entropy, logic_depth, computed_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(chunk_id) DO UPDATE SET "
                "projections = excluded.projections, "
                "entropy = excluded.entropy, "
                "logic_depth = excluded.logic_depth, "
                "computed_at = excluded.computed_at",
                (
                    int(chunk_id),
                    blob,
                    float(entropy),
                    float(logic_depth),
                    _now_unix_s(),
                ),
            )
            await self._conn.commit()

    async def get_chunk_epa(self, chunk_id: int) -> ChunkEpaRow | None:
        async with self._conn.execute(
            "SELECT chunk_id, projections, entropy, logic_depth, computed_at "
            "FROM chunk_epa WHERE chunk_id = ?",
            (int(chunk_id),),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return ChunkEpaRow(
            chunk_id=int(row[0]),
            projections=blob_to_f32_vec(row[1]) or [],
            entropy=float(row[2]),
            logic_depth=float(row[3]),
            computed_at=int(row[4]),
        )

    # ------------------------------------------------------------------
    # namespace helpers (schema v5)
    # ------------------------------------------------------------------

    async def list_namespaces(self) -> list[tuple[str, int]]:
        """List every distinct ``chunks.namespace`` + its row count, asc by name."""

        async with self._conn.execute(
            "SELECT namespace, COUNT(*) AS n FROM chunks "
            "GROUP BY namespace ORDER BY namespace ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [(str(r[0]), max(0, int(r[1]))) for r in rows]

    async def filter_chunk_ids_by_namespace(self, namespaces: Sequence[str]) -> list[int]:
        """Chunks whose ``namespace`` is in the list, sorted by id.

        Empty ``namespaces`` ⇒ returns every chunk id.
        """

        if not namespaces:
            async with self._conn.execute(
                "SELECT id FROM chunks ORDER BY id ASC"
            ) as cursor:
                rows = await cursor.fetchall()
            return [int(r[0]) for r in rows]
        sql = (
            f"SELECT id FROM chunks WHERE namespace IN ({_placeholders(len(namespaces))}) "
            f"ORDER BY id ASC"
        )
        async with self._conn.execute(sql, tuple(namespaces)) as cursor:
            rows = await cursor.fetchall()
        return [int(r[0]) for r in rows]

    # ------------------------------------------------------------------
    # kv_store
    # ------------------------------------------------------------------

    async def kv_get(self, key: str) -> str | None:
        async with self._conn.execute(
            "SELECT value FROM kv_store WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return None if row[0] is None else str(row[0])

    async def kv_set(self, key: str, value: str) -> None:
        async with self._lock:
            await self._conn.execute(
                "INSERT OR REPLACE INTO kv_store(key, value, vector) VALUES (?, ?, NULL)",
                (key, value),
            )
            await self._conn.commit()

    async def table_exists(self, name: str) -> bool:
        async with self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
            (name,),
        ) as cursor:
            row = await cursor.fetchone()
        return row is not None


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _row_to_chunk(r: Iterable) -> ChunkRow:
    fields = list(r)
    return ChunkRow(
        id=int(fields[0]),
        file_id=int(fields[1]),
        chunk_index=int(fields[2]),
        content=str(fields[3]),
        vector=blob_to_f32_vec(fields[4]),
        namespace=str(fields[5]),
    )
