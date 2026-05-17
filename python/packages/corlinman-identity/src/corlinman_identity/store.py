"""Schema constants + the :class:`SqliteIdentityStore` handle.

Direct port of ``rust/crates/corlinman-identity/src/store.rs``. The
schema string is byte-identical with the Rust ``SCHEMA_SQL`` so a
Python ``open`` and a Rust ``open`` on the same file produce the same
tables and indexes (and either side can be swapped in later without a
migration).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite

from corlinman_identity.error import OpenError, StorageError
from corlinman_identity.tenancy import TenantIdLike, tenant_db_path

# ---------------------------------------------------------------------------
# Schema — three tables; matches the Rust ``SCHEMA_SQL`` 1:1.
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user_identities (
    user_id TEXT PRIMARY KEY,
    display_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS user_aliases (
    channel TEXT NOT NULL,
    channel_user_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    binding_kind TEXT NOT NULL,
    PRIMARY KEY (channel, channel_user_id),
    FOREIGN KEY (user_id) REFERENCES user_identities(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_aliases_user_id ON user_aliases(user_id);

CREATE TABLE IF NOT EXISTS verification_phrases (
    phrase TEXT PRIMARY KEY,
    issued_to_user_id TEXT NOT NULL,
    issued_on_channel TEXT NOT NULL,
    issued_on_channel_user_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    consumed_on_channel TEXT,
    consumed_on_channel_user_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_verification_phrases_expires
    ON verification_phrases(expires_at);
"""


def identity_db_path(data_dir: Path, tenant: TenantIdLike | str) -> Path:
    """Resolve the per-tenant ``user_identity.sqlite`` path under ``data_dir``.

    Uses the same convention the gateway uses for every other tenant DB
    (``<data_dir>/tenants/<slug>/<db>.sqlite``). When the tenant is the
    legacy default this collapses to the unscoped path segment,
    matching ``corlinman-replay::sessions_db_path``'s behaviour.

    TODO(tenancy-integration): once ``corlinman_server.tenancy`` (or
    the canonical Python ``corlinman-tenant`` package) ships, this
    function should defer to the canonical
    ``corlinman_tenant.tenant_db_path``. The slug → path mapping must
    stay byte-identical with the Rust implementation.
    """
    return tenant_db_path(data_dir, tenant, "user_identity")


class SqliteIdentityStore:
    """SQLite-backed identity store.

    Owns one shared :class:`aiosqlite.Connection`. SQLite serialises
    writes internally, so a single shared connection mirrors the
    Rust crate's ``open_with_pool_size(1)`` test convention and
    dodges the WAL cross-connection visibility race the rest of the
    workspace's per-tenant stores have documented (see
    ``EvolutionStore::open_with_pool_size`` for the same pattern).

    Async lifecycle:

    * :meth:`open` does the file-create + schema apply and returns a
      ready-to-use store. Caller eventually awaits :meth:`close`.
    * Most callers wrap the store in ``async with`` so the connection
      drops cleanly even if a downstream coroutine raises.
    """

    def __init__(self, conn: aiosqlite.Connection, path: Path) -> None:
        self._conn = conn
        self._path = path
        # Serialises ``BEGIN`` / ``COMMIT`` blocks across interleaved
        # coroutines. The Rust crate sidesteps this by running each
        # request on its own pool connection; we hold a single shared
        # ``aiosqlite`` connection, so we have to gate the multi-stmt
        # transactional paths (``resolve_or_create``, ``redeem_phrase``,
        # ``merge_users``) ourselves. Single-statement reads / writes
        # bypass the lock and rely on SQLite's internal serialisation.
        self._tx_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    async def open(cls, path: Path) -> SqliteIdentityStore:
        """Open (or create) the identity DB at ``path``.

        Opens with WAL + ``synchronous=NORMAL`` for write throughput
        and applies :data:`SCHEMA_SQL` so callers never have to run
        migrations by hand. Idempotent — re-opening an already-
        bootstrapped file is safe (each ``CREATE TABLE`` is
        ``IF NOT EXISTS``).
        """
        return await cls.open_with_pool_size(path, 1)

    @classmethod
    async def open_with_pool_size(
        cls, path: Path, max_connections: int
    ) -> SqliteIdentityStore:
        """As :meth:`open`, but accepts an explicit ``max_connections``.

        Kept for parity with the Rust signature. ``aiosqlite`` is a
        thin asyncio wrapper around a single sqlite3 connection per
        instance, so the parameter is informational here — the Rust
        side's pool-size knob exists to dodge the WAL cross-conn
        visibility race, which a single shared connection sidesteps
        by construction.
        """
        _ = max_connections  # kept for signature compatibility with the Rust crate
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # ``isolation_level=None`` puts the underlying ``sqlite3``
            # connection into autocommit mode so explicit ``BEGIN`` /
            # ``COMMIT`` we issue from the resolver / verification
            # paths actually start and end a transaction. Without
            # this, sqlite3's default isolation_level wraps every
            # ``execute`` in an implicit transaction and our explicit
            # ``BEGIN`` raises "cannot start a transaction within a
            # transaction".
            conn = await aiosqlite.connect(str(path), isolation_level=None)
        except Exception as exc:  # pragma: no cover - aiosqlite open is reliable
            raise OpenError(path=path, source=exc) from exc

        try:
            # WAL + NORMAL synchronous matches the Rust SqliteConnectOptions
            # config: better write throughput, same durability story for
            # a per-tenant identity graph (small, append-mostly).
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.execute("PRAGMA synchronous = NORMAL")
            # Foreign keys default OFF in SQLite; the ``ON DELETE CASCADE``
            # on ``user_aliases`` (and the orphan-cleanup story it props
            # up) is load-bearing for the merge path.
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.executescript(SCHEMA_SQL)
            await conn.commit()
        except Exception as exc:
            await conn.close()
            raise StorageError(op="apply_schema", source=exc) from exc

        return cls(conn=conn, path=path)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying connection. Idempotent."""
        if self._conn is not None:
            await self._conn.close()
            # Drop reference but keep the attribute so re-entrant
            # ``close()`` calls don't AttributeError.
            self._conn = None  # type: ignore[assignment]

    async def __aenter__(self) -> SqliteIdentityStore:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @property
    def conn(self) -> aiosqlite.Connection:
        """Borrow the underlying connection.

        Crate-private equivalent — kept ``public`` in Python because
        the verification module reaches in to run its own queries.
        External callers should go through :class:`IdentityStore`.
        """
        if self._conn is None:
            raise RuntimeError("SqliteIdentityStore used after close()")
        return self._conn

    @property
    def path(self) -> Path:
        return self._path

    @property
    def tx_lock(self) -> asyncio.Lock:
        """Lock that guards multi-statement transactions on the shared
        connection. The resolver and verification modules acquire this
        around their ``BEGIN..COMMIT`` blocks."""
        return self._tx_lock


__all__ = [
    "SCHEMA_SQL",
    "SqliteIdentityStore",
    "identity_db_path",
]
