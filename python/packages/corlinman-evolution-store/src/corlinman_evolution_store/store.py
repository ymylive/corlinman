"""Thin SQLite wrapper for the EvolutionLoop DB.

Ported from ``rust/crates/corlinman-evolution/src/store.rs``. Opens (or
creates) the evolution SQLite file and applies :data:`SCHEMA_SQL` plus
the column migrations idempotently.

WAL journal + ``synchronous=NORMAL`` + ``foreign_keys=ON`` to match the
Rust crate. The Python sibling :mod:`corlinman_evolution_engine` reads
the same file via ``aiosqlite``; the schema is the cross-language
contract.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from corlinman_evolution_store.schema import MIGRATIONS, POST_MIGRATIONS_SQL, SCHEMA_SQL


class OpenError(RuntimeError):
    """Raised when the store cannot open the SQLite file or apply the
    schema / migrations. Mirrors the Rust ``OpenError`` enum — the
    Python surface collapses the variants into one exception with a
    stable, prefixed message so callers can pattern-match the prefix
    if they need to."""


async def _column_exists(conn: aiosqlite.Connection, table: str, column: str) -> bool:
    """``True`` iff ``table.column`` exists in the database.

    Backed by SQLite's ``pragma_table_info`` virtual table — same probe
    the Rust crate uses (and same f-string-into-SQL caveat: only call
    this with ``'static`` table / column names sourced from
    :data:`MIGRATIONS`, never user input).
    """
    safe_table = table.replace("'", "''")
    cursor = await conn.execute(
        f"SELECT 1 FROM pragma_table_info('{safe_table}') WHERE name = ?",
        (column,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row is not None


class EvolutionStore:
    """Async wrapper around ``evolution.sqlite``.

    Use as an async context manager (``async with``) or call
    :meth:`open` directly and remember to ``await store.close()``. The
    underlying ``aiosqlite.Connection`` is exposed via :attr:`conn` so
    repos can share it.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @classmethod
    async def open(cls, path: Path) -> EvolutionStore:
        """Open (or create) the evolution SQLite at ``path``. WAL +
        ``synchronous=NORMAL`` + ``foreign_keys=ON``. Applies
        :data:`SCHEMA_SQL` + :data:`MIGRATIONS` + :data:`POST_MIGRATIONS_SQL`
        once — ``CREATE … IF NOT EXISTS`` makes this safe to repeat."""
        store = cls(path)
        await store._open()
        return store

    async def __aenter__(self) -> EvolutionStore:
        await self._open()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def _open(self) -> None:
        try:
            conn = await aiosqlite.connect(self._path)
        except Exception as exc:  # pragma: no cover - aiosqlite raises sqlite3.Error
            raise OpenError(f"connect '{self._path}': {exc}") from exc
        try:
            # PRAGMAs to match the Rust SqliteConnectOptions.
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.execute("PRAGMA synchronous = NORMAL")
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.execute("PRAGMA busy_timeout = 5000")

            try:
                await conn.executescript(SCHEMA_SQL)
            except Exception as exc:
                raise OpenError(f"apply SCHEMA_SQL: {exc}") from exc

            # Idempotent column migrations.
            for table, column, ddl in MIGRATIONS:
                if not await _column_exists(conn, table, column):
                    try:
                        await conn.execute(ddl)
                    except Exception as exc:
                        raise OpenError(
                            f"apply migration {table}.{column}: {exc}"
                        ) from exc

            # Indexes that reference migrated columns must be created
            # *after* the migrations loop has added those columns.
            try:
                await conn.executescript(POST_MIGRATIONS_SQL)
            except Exception as exc:
                raise OpenError(f"apply POST_MIGRATIONS_SQL: {exc}") from exc

            await conn.commit()
        except OpenError:
            await conn.close()
            raise
        except Exception as exc:
            await conn.close()
            raise OpenError(f"open '{self._path}': {exc}") from exc

        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("EvolutionStore used outside async context (call open() first)")
        return self._conn


__all__ = ["EvolutionStore", "OpenError"]
