"""SQLite access for ``user_model.sqlite``.

This package owns the schema (unlike ``corlinman-evolution-engine``,
which only consumes a Rust-owned DB). We create the table on first open
so the CLI can be invoked against an empty data dir without a separate
migration step.

All times are unix milliseconds (i64) for parity with the rest of the
corlinman Python plane.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from corlinman_user_model.traits import TraitKind, UserTrait

# ---------------------------------------------------------------------------
# Schema — see docs/design/phase3-roadmap.md §5 for the canonical version.
#
# Phase 3.1 adds ``tenant_id`` (default ``'default'``). Phase 4 will switch
# the multi-tenant gateway over to real tenant ids, at which point every
# ``user_id`` query implicitly scoped under ``'default'`` becomes a single
# call-site change instead of a schema migration on populated user data.
# Doing it now while the table is small is ~10x cheaper than later.
# ---------------------------------------------------------------------------

# The tenant_id default lets every existing call site keep working without
# a parameter — only Phase 4's multi-tenant fan-out has to thread real ids.
DEFAULT_TENANT_ID = "default"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user_traits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT NOT NULL DEFAULT 'default',
    user_id      TEXT NOT NULL,
    trait_kind   TEXT NOT NULL,
    trait_value  TEXT NOT NULL,
    confidence   REAL NOT NULL,
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL,
    session_ids  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_traits_user
    ON user_traits(user_id);

CREATE INDEX IF NOT EXISTS idx_user_traits_user_kind
    ON user_traits(user_id, trait_kind);

CREATE INDEX IF NOT EXISTS idx_user_traits_tenant_user
    ON user_traits(tenant_id, user_id);
"""


# Migrations applied after :data:`_SCHEMA_SQL`. Each entry is
# ``(table, column, ddl)``. The runtime checks via ``PRAGMA table_info``
# whether the column exists before running the ALTER, so this is
# idempotent on both fresh DBs (CREATE TABLE has the column already and
# the ALTER is skipped) and pre-Phase-3.1 DBs (CREATE TABLE was the old
# shape; the ALTER adds the column).
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    (
        "user_traits",
        "tenant_id",
        "ALTER TABLE user_traits ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
    ),
)


async def _column_exists(conn: aiosqlite.Connection, table: str, column: str) -> bool:
    """True iff ``table.column`` exists. Used by the migration runner.

    SQLite has no `ADD COLUMN IF NOT EXISTS`; we pragma-check first so
    re-opens stay idempotent. Mirrors the Rust crate's
    ``corlinman-evolution::store::column_exists`` shape.
    """
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    await cursor.close()
    return any(str(r[1]) == column for r in rows)


# Weighted-average constants for the upsert path. Old confidence keeps
# 70% weight, the new observation gets 30%. This makes the score
# converge slowly enough to absorb noisy single-session readings while
# still moving when the user's behaviour drifts.
_OLD_WEIGHT = 0.7
_NEW_WEIGHT = 0.3


class UserModelStore:
    """Async wrapper around ``user_model.sqlite``.

    Used as an async context manager so the connection closes cleanly
    even if the distill loop aborts mid-batch. Mirrors the shape of
    ``EvolutionStore`` in ``corlinman-evolution-engine``.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @classmethod
    async def open_or_create(cls, path: Path) -> UserModelStore:
        """Construct a store and run schema bootstrap.

        Caller must still ``__aenter__`` / ``__aexit__`` to manage the
        connection — this constructor only ensures the file + schema
        exist on disk. Idempotent migrations land here too so a
        pre-Phase-3.1 ``user_model.sqlite`` picks up the ``tenant_id``
        column automatically on first re-open.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        # Open synchronously through aiosqlite to apply the schema, then
        # close — the caller will re-open via the context manager.
        async with aiosqlite.connect(path) as conn:
            await conn.executescript(_SCHEMA_SQL)
            for table, column, ddl in _MIGRATIONS:
                if not await _column_exists(conn, table, column):
                    await conn.execute(ddl)
            await conn.commit()
        return cls(path)

    async def __aenter__(self) -> UserModelStore:
        self._conn = await aiosqlite.connect(self._path)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("UserModelStore used outside async context")
        return self._conn

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def list_traits_for_user(
        self,
        user_id: str,
        *,
        kind: TraitKind | None = None,
        min_confidence: float = 0.4,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> list[UserTrait]:
        """Return traits for ``user_id`` ordered by confidence DESC.

        ``min_confidence`` matches the
        ``[user_model] trait_confidence_floor`` config default; callers
        can lower it for admin views and raise it for prompt-time
        rendering. ``tenant_id`` defaults to ``'default'`` — Phase 4
        multi-tenant fan-out flips that one parameter at the call site.
        """
        if kind is None:
            cursor = await self.conn.execute(
                """SELECT user_id, trait_kind, trait_value, confidence,
                          first_seen, last_seen, session_ids
                   FROM user_traits
                   WHERE tenant_id = ? AND user_id = ? AND confidence >= ?
                   ORDER BY confidence DESC, last_seen DESC""",
                (tenant_id, user_id, min_confidence),
            )
        else:
            cursor = await self.conn.execute(
                """SELECT user_id, trait_kind, trait_value, confidence,
                          first_seen, last_seen, session_ids
                   FROM user_traits
                   WHERE tenant_id = ? AND user_id = ? AND trait_kind = ?
                     AND confidence >= ?
                   ORDER BY confidence DESC, last_seen DESC""",
                (tenant_id, user_id, kind.value, min_confidence),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_trait(r) for r in rows]

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def upsert_trait(
        self,
        *,
        user_id: str,
        trait_kind: TraitKind,
        trait_value: str,
        confidence: float,
        session_id: str,
        now_ms: int | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> None:
        """Insert a fresh trait, or update an existing one in place.

        Matching is by ``(tenant_id, user_id, trait_kind, trait_value)``.
        On match we take the weighted average ``0.7 * old + 0.3 * new``,
        bump ``last_seen``, and append ``session_id`` to the JSON array
        if not already present. Plain overwrite would lose the smoothing
        the design calls for. ``tenant_id`` defaults to ``'default'``
        until Phase 4 wires real tenant ids.
        """
        if now_ms is None:
            now_ms = int(time.time() * 1_000)
        cursor = await self.conn.execute(
            """SELECT id, confidence, first_seen, session_ids
               FROM user_traits
               WHERE tenant_id = ? AND user_id = ? AND trait_kind = ?
                 AND trait_value = ?""",
            (tenant_id, user_id, trait_kind.value, trait_value),
        )
        existing = await cursor.fetchone()
        await cursor.close()

        if existing is None:
            await self.conn.execute(
                """INSERT INTO user_traits
                     (tenant_id, user_id, trait_kind, trait_value, confidence,
                      first_seen, last_seen, session_ids)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tenant_id,
                    user_id,
                    trait_kind.value,
                    trait_value,
                    _clamp_confidence(confidence),
                    now_ms,
                    now_ms,
                    json.dumps([session_id]),
                ),
            )
            await self.conn.commit()
            return

        row_id = int(existing[0])
        old_conf = float(existing[1])
        # ``first_seen`` is preserved; we only ever move ``last_seen`` forward.
        existing_session_ids = _decode_session_ids(existing[3])
        if session_id not in existing_session_ids:
            existing_session_ids.append(session_id)
        new_conf = _clamp_confidence(_OLD_WEIGHT * old_conf + _NEW_WEIGHT * confidence)
        await self.conn.execute(
            """UPDATE user_traits
               SET confidence = ?, last_seen = ?, session_ids = ?
               WHERE id = ?""",
            (new_conf, now_ms, json.dumps(existing_session_ids), row_id),
        )
        await self.conn.commit()

    async def prune_low_confidence(self, floor: float) -> int:
        """Delete traits with ``confidence < floor``. Returns rows deleted.

        Used by the housekeeping CLI after a redistill: as confidences
        decay below the floor, the trait stops being interesting and the
        row just wastes space.
        """
        cursor = await self.conn.execute(
            "DELETE FROM user_traits WHERE confidence < ?",
            (floor,),
        )
        deleted = cursor.rowcount or 0
        await cursor.close()
        await self.conn.commit()
        return int(deleted)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp_confidence(value: float) -> float:
    """Confidence is always in ``[0.0, 1.0]``."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _decode_session_ids(raw: object) -> list[str]:
    """Parse the JSON-encoded ``session_ids`` column.

    Defensive: a corrupted row shouldn't break the whole upsert. We
    return an empty list in that case and the caller appends the new
    session_id, which silently repairs the row on next write.
    """
    if not isinstance(raw, str) or not raw:
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [str(x) for x in decoded]


def _row_to_trait(row: aiosqlite.Row | tuple[Any, ...]) -> UserTrait:
    """Map a SELECT row to a :class:`UserTrait`.

    Unknown trait_kind values fall back to ``TraitKind.TOPIC`` rather
    than crashing — this can only happen if the DB was written by an
    older / future version of this package.
    """
    kind = TraitKind.parse(str(row[1])) or TraitKind.TOPIC
    session_ids = tuple(_decode_session_ids(row[6]))
    return UserTrait(
        user_id=str(row[0]),
        trait_kind=kind,
        trait_value=str(row[2]),
        confidence=float(row[3]),
        first_seen=int(row[4]),
        last_seen=int(row[5]),
        session_ids=session_ids,
    )


__all__ = ["DEFAULT_TENANT_ID", "UserModelStore"]
