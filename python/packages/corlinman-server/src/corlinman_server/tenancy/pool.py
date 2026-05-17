"""``TenantPool`` — multi-DB pool wrapper keyed by ``(TenantId, db_name)``.

Python port of ``corlinman-tenant::pool``. The Rust crate hands out
``sqlx::SqlitePool`` (a connection pool); :mod:`aiosqlite` exposes
single connections, not pools, so the Python port stores one cached
:class:`aiosqlite.Connection` per ``(tenant, db_name)`` pair and serves
that to callers — preserving the "lazy-open + cache + share" contract
even though the underlying primitive differs.

Pragmas applied at open match the Rust side:

* ``journal_mode=WAL`` — concurrent readers + a single writer,
* ``synchronous=NORMAL`` — durability on power-loss, but skips fsync on
  every commit (the chosen point on the durability/throughput curve for
  the rest of the corlinman SQLite stores),
* ``foreign_keys=ON`` — enforced by SQLite per-connection,
* ``busy_timeout=5000`` — 5-second wait on contention before raising.

Concurrency: the cache is guarded by an :class:`asyncio.Lock`, locked
only for the "find or insert" step. The first-open of a single
``(tenant, db_name)`` pair runs under the lock so two concurrent opens
of the same file don't race in WAL setup — exactly the Phase 4 W1.5
/A7 flake fix the Rust side ships.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import NamedTuple

import aiosqlite

from corlinman_server.tenancy.id import TenantId
from corlinman_server.tenancy.path import tenant_db_path, tenant_root_dir


class TenantPoolError(RuntimeError):
    """Base class for :meth:`TenantPool.get_or_open` errors."""


class TenantPoolCreateDirError(TenantPoolError):
    """The tenant's directory could not be created (permission, full
    disk, parent missing, etc). Wraps the underlying
    :class:`OSError`."""

    def __init__(self, path: Path, source: OSError) -> None:
        self.path = path
        self.source = source
        super().__init__(f"create tenant dir {path}: {source}")


class TenantPoolConnectError(TenantPoolError):
    """Connection open or initial pragma application failed. Wraps the
    underlying :class:`aiosqlite.Error`."""

    def __init__(self, db_path: Path, source: BaseException) -> None:
        self.db_path = db_path
        self.source = source
        super().__init__(f"connect sqlite {db_path}: {source}")


class _Key(NamedTuple):
    """Composite cache key. Private — the public API takes
    ``(TenantId, str)`` and we materialise the key internally so the
    surface doesn't leak the tuple shape."""

    tenant: TenantId
    db_name: str


class TenantPool:
    """Multi-DB connection wrapper. Cheap to share across coroutines;
    every instance shares its own cache via the internal lock and
    dict — clone semantics from the Rust ``Arc`` version are not
    needed in Python (just hand the instance around).

    Connections opened through :meth:`get_or_open` are cached forever
    (process lifetime) unless explicitly closed via :meth:`close_all`.
    """

    # Connection budget per `(tenant, db_name)` cached entry. The Rust
    # side allocates an N-connection pool here (default 8); aiosqlite
    # serves a single async connection per file, so the cap is "1
    # connection per pair". Kept as an attribute for parity with the
    # builder API and so a future port to a real pool (e.g.
    # ``sqlalchemy.ext.asyncio``) can flip it.
    _DEFAULT_MAX_CONNECTIONS = 8

    def __init__(self, root: Path | str) -> None:
        """Build an empty wrapper rooted at ``root``. Connections
        open lazily on first :meth:`get_or_open` — an empty wrapper is
        cheap so tests / stripped-down builds don't pay any I/O they
        won't use."""
        self._root: Path = Path(root)
        self._cache: dict[_Key, aiosqlite.Connection] = {}
        self._lock = asyncio.Lock()
        self._max_connections = self._DEFAULT_MAX_CONNECTIONS

    # ---- builders --------------------------------------------------------------

    def with_max_connections(self, n: int) -> TenantPool:
        """Override the per-pool connection cap (default 8). Mirrors the
        Rust builder; in the aiosqlite port the value is stored for
        parity but not yet enforced (one connection per pair is the
        only shape aiosqlite supports without a pool layer)."""
        self._max_connections = n
        return self

    # ---- accessors -------------------------------------------------------------

    def root(self) -> Path:
        """Filesystem root the wrapper was opened against. Tests use
        this to assert connection paths landed in the expected tempdir."""
        return self._root

    def db_path(self, tenant: TenantId, db_name: str) -> Path:
        """Resolved path for ``(tenant, db_name)``. Does **not** create
        the file — exposed so admin / migration code can probe whether
        the file already exists before deciding to open."""
        return tenant_db_path(self._root, tenant, db_name)

    async def is_cached(self, tenant: TenantId, db_name: str) -> bool:
        """True iff the connection for ``(tenant, db_name)`` is already
        cached. Tests use this to assert lazy-open behaviour without
        forcing a connection. Public so the gateway's ``tenant create``
        path can short-circuit a re-open after migrations."""
        key = _Key(tenant, db_name)
        async with self._lock:
            return key in self._cache

    # ---- main entrypoint -------------------------------------------------------

    async def get_or_open(self, tenant: TenantId, db_name: str) -> aiosqlite.Connection:
        """Open (or return cached) :class:`aiosqlite.Connection` for
        ``(tenant, db_name)``.

        Creates the parent directory tree on first open so callers
        don't have to mkdir before invoking us. WAL +
        ``synchronous=NORMAL`` + ``foreign_keys=ON`` matches the rest
        of the codebase.

        Raises:
          * :class:`TenantPoolCreateDirError` — directory create failed.
          * :class:`TenantPoolConnectError` — connection open / pragma
            apply failed.
        """
        key = _Key(tenant, db_name)

        # Fast path: already cached. Held briefly to avoid blocking
        # unrelated opens.
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached

            # Slow path: serialise open of this (tenant, db) pair under
            # the lock. Earlier Rust revisions opened *outside* the
            # lock to keep distinct-pair opens parallel, but Phase 4
            # W1.5 /A7 caught a flake where two simultaneous SQLite
            # opens of the *same* file race in `create_if_missing` +
            # WAL setup and one transiently errors. The fix —
            # serialise — also simplifies the Python port (one
            # `asyncio.Lock`, no double-checked locking, no risk of
            # awaiting `connect` outside the lock and then losing the
            # race anyway).
            dir_path = tenant_root_dir(self._root, tenant)
            try:
                dir_path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise TenantPoolCreateDirError(dir_path, exc) from exc

            file_path = tenant_db_path(self._root, tenant, db_name)
            try:
                conn = await aiosqlite.connect(str(file_path))
                # Pragmas mirror the Rust SqliteConnectOptions chain.
                # WAL + NORMAL is the codebase default; foreign_keys
                # is per-connection so we set it on every open;
                # busy_timeout absorbs short contention windows
                # without surfacing SQLITE_BUSY to the caller.
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA synchronous=NORMAL")
                await conn.execute("PRAGMA foreign_keys=ON")
                await conn.execute("PRAGMA busy_timeout=5000")
                # Default row factory: aiosqlite returns tuples; the
                # admin_schema layer accesses columns positionally,
                # so we leave the factory alone here. Callers that
                # want dict-style access can swap `conn.row_factory`
                # at the call site.
            except BaseException as exc:
                # Cover both aiosqlite.Error and the underlying
                # sqlite3.OperationalError surfaces — wrap them so
                # callers don't have to import the aiosqlite tree.
                raise TenantPoolConnectError(file_path, exc) from exc

            self._cache[key] = conn
            return conn

    # ---- lifecycle -------------------------------------------------------------

    async def close_all(self) -> None:
        """Close every cached connection and drop the cache. Idempotent
        — calling again on an already-drained pool is a no-op. Used at
        process shutdown so SQLite's WAL files get checkpointed
        cleanly; tests that want to assert "no connections leaked"
        also call this in teardown."""
        async with self._lock:
            connections = list(self._cache.values())
            self._cache.clear()
        # Close outside the lock so a slow close (WAL checkpoint, etc.)
        # doesn't keep new opens waiting. Surfacing one bad connection
        # close shouldn't mask the others — the Rust side relies on
        # sqlx's drop to clean up; the Python equivalent is best-effort.
        for conn in connections:
            with contextlib.suppress(Exception):
                await conn.close()


__all__ = [
    "TenantPool",
    "TenantPoolConnectError",
    "TenantPoolCreateDirError",
    "TenantPoolError",
]
