"""Async SQLite store for ``agent_state.sqlite``.

Mirrors the schema defined in ``docs/design/phase3-roadmap.md`` §5. The
store owns the schema (no Rust crate writes here yet) so we ``CREATE TABLE
IF NOT EXISTS`` on open and let the file appear on first use.

All times are unix milliseconds.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from corlinman_persona.state import RECENT_TOPICS_CAP, PersonaState

# ``tenant_id`` defaults to ``'default'`` for single-tenant callers, and
# participates in the primary key so the same ``agent_id`` can coexist
# across tenants without overwriting state.
DEFAULT_TENANT_ID = "default"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_persona_state (
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    agent_id      TEXT NOT NULL,
    mood          TEXT NOT NULL DEFAULT 'neutral',
    fatigue       REAL NOT NULL DEFAULT 0.0,
    recent_topics TEXT NOT NULL DEFAULT '[]',
    updated_at    INTEGER NOT NULL,
    state_json    TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_persona_tenant_agent
    ON agent_persona_state(tenant_id, agent_id);
"""

# Idempotent column migrations. Each entry is
# ``(table, column, ddl)`` — the runtime pragma-checks for the column
# before running the ALTER. SQLite has no `ADD COLUMN IF NOT EXISTS`,
# so we mirror the Rust crate's ``column_exists`` pattern.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    (
        "agent_persona_state",
        "tenant_id",
        "ALTER TABLE agent_persona_state ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
    ),
)


async def _table_exists(conn: aiosqlite.Connection, table: str) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row is not None


async def _column_exists(conn: aiosqlite.Connection, table: str, column: str) -> bool:
    """True iff ``table.column`` exists. Used by the migration runner."""
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    await cursor.close()
    return any(str(r[1]) == column for r in rows)


async def _primary_key_columns(conn: aiosqlite.Connection, table: str) -> list[str]:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    await cursor.close()
    return [str(r[1]) for r in sorted((r for r in rows if int(r[5]) > 0), key=lambda r: r[5])]


async def _migrate_agent_persona_schema(conn: aiosqlite.Connection) -> None:
    """Ensure ``agent_persona_state`` uses ``(tenant_id, agent_id)`` as PK."""
    if not await _table_exists(conn, "agent_persona_state"):
        await conn.executescript(SCHEMA_SQL)
        return

    for table, column, ddl in _MIGRATIONS:
        if not await _column_exists(conn, table, column):
            await conn.execute(ddl)

    if await _primary_key_columns(conn, "agent_persona_state") == [
        "tenant_id",
        "agent_id",
    ]:
        await conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_agent_persona_tenant_agent
               ON agent_persona_state(tenant_id, agent_id)"""
        )
        return

    await conn.executescript(
        """
        DROP TABLE IF EXISTS agent_persona_state_migration_old;
        ALTER TABLE agent_persona_state RENAME TO agent_persona_state_migration_old;
        CREATE TABLE agent_persona_state (
            tenant_id     TEXT NOT NULL DEFAULT 'default',
            agent_id      TEXT NOT NULL,
            mood          TEXT NOT NULL DEFAULT 'neutral',
            fatigue       REAL NOT NULL DEFAULT 0.0,
            recent_topics TEXT NOT NULL DEFAULT '[]',
            updated_at    INTEGER NOT NULL,
            state_json    TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (tenant_id, agent_id)
        );
        INSERT INTO agent_persona_state
            (tenant_id, agent_id, mood, fatigue, recent_topics, updated_at, state_json)
        SELECT tenant_id, agent_id, mood, fatigue, recent_topics, updated_at, state_json
        FROM agent_persona_state_migration_old;
        DROP TABLE agent_persona_state_migration_old;
        CREATE INDEX IF NOT EXISTS idx_agent_persona_tenant_agent
            ON agent_persona_state(tenant_id, agent_id);
        """
    )


def _now_ms() -> int:
    """Unix milliseconds; pulled out so tests can monkeypatch if needed."""
    return int(time.time() * 1000)


def _dedup_cap(topics: list[str]) -> list[str]:
    """Keep last occurrence of each topic and trim to :data:`RECENT_TOPICS_CAP`.

    ``["a", "b", "a", "c"]`` → ``["b", "a", "c"]``. Tail-truncated rather
    than head-truncated so the most recent activity always wins.
    """
    seen: dict[str, int] = {}
    for idx, topic in enumerate(topics):
        seen[topic] = idx
    # Stable order: by index of last occurrence, ascending.
    ordered = sorted(seen.items(), key=lambda item: item[1])
    deduped = [topic for topic, _ in ordered]
    if len(deduped) > RECENT_TOPICS_CAP:
        deduped = deduped[-RECENT_TOPICS_CAP:]
    return deduped


def _decode_topics(raw: str) -> list[str]:
    """Decode the JSON-encoded ``recent_topics`` column.

    Bad JSON or non-list payloads degrade to an empty list rather than
    raising — the decay job runs unattended and we'd rather lose history
    than crash on a corrupted row.
    """
    try:
        parsed = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(t) for t in parsed]


def _decode_state_json(raw: str) -> dict[str, Any]:
    """Decode the free-form ``state_json`` column.

    Same defensive posture as :func:`_decode_topics` — corrupted blobs
    become empty dicts so the resolver returns empty strings instead of
    propagating an exception into prompt rendering.
    """
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _row_to_state(row: aiosqlite.Row | tuple[Any, ...]) -> PersonaState:
    return PersonaState(
        agent_id=str(row[0]),
        mood=str(row[1]),
        fatigue=float(row[2]),
        recent_topics=_decode_topics(str(row[3])),
        updated_at_ms=int(row[4]),
        state_json=_decode_state_json(str(row[5])),
    )


class PersonaStore:
    """Async wrapper around ``agent_state.sqlite``.

    Use as an async context manager so the connection closes cleanly even
    if the caller aborts mid-task.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @classmethod
    async def open_or_create(cls, path: Path) -> PersonaStore:
        """Open the DB (creating the file + schema if absent) and return
        an entered store.

        Convenience for callers that don't need ``async with`` framing
        (CLI subcommands, single-shot tests). The caller is responsible
        for ``await store.close()``.
        """
        store = cls(path)
        await store._open()
        return store

    async def __aenter__(self) -> PersonaStore:
        await self._open()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def _open(self) -> None:
        # ``aiosqlite.connect`` will create the file on demand. The
        # schema helper also rewrites legacy ``PRIMARY KEY(agent_id)``
        # tables into the current composite-key shape.
        self._conn = await aiosqlite.connect(self._path)
        await _migrate_agent_persona_schema(self._conn)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("PersonaStore used outside async context")
        return self._conn

    async def get(
        self,
        agent_id: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> PersonaState | None:
        """Return the row for ``(tenant_id, agent_id)`` or ``None``.

        ``tenant_id`` defaults to ``'default'`` for older call sites.
        """
        cursor = await self.conn.execute(
            """SELECT agent_id, mood, fatigue, recent_topics,
                      updated_at, state_json
               FROM agent_persona_state
               WHERE tenant_id = ? AND agent_id = ?""",
            (tenant_id, agent_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return _row_to_state(row)

    async def upsert(
        self,
        state: PersonaState,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> None:
        """Insert or replace the row for ``(tenant_id, state.agent_id)``.

        ``recent_topics`` is dedup'd and capped on write so callers can't
        accidentally bypass the invariant by hand-crafting a long list.
        ``updated_at_ms`` is bumped to "now" if the caller passed 0,
        otherwise we trust their timestamp (lets tests exercise decay
        with deterministic clocks).
        """
        capped = _dedup_cap(list(state.recent_topics))
        updated_at = state.updated_at_ms or _now_ms()
        await self.conn.execute(
            """INSERT INTO agent_persona_state
                 (tenant_id, agent_id, mood, fatigue, recent_topics,
                  updated_at, state_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tenant_id, agent_id) DO UPDATE SET
                 mood          = excluded.mood,
                 fatigue       = excluded.fatigue,
                 recent_topics = excluded.recent_topics,
                 updated_at    = excluded.updated_at,
                 state_json    = excluded.state_json""",
            (
                tenant_id,
                state.agent_id,
                state.mood,
                float(state.fatigue),
                json.dumps(capped),
                updated_at,
                json.dumps(state.state_json),
            ),
        )
        await self.conn.commit()

    async def update_mood(
        self,
        agent_id: str,
        mood: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> None:
        """Set the ``mood`` column for ``(tenant_id, agent_id)``.

        Silently no-ops if the row doesn't exist — callers who need
        seeding should go through :func:`~corlinman_persona.seeder.seed_from_card`.
        Mutations on existing rows otherwise belong to the EvolutionLoop.
        """
        await self.conn.execute(
            """UPDATE agent_persona_state
               SET mood = ?, updated_at = ?
               WHERE tenant_id = ? AND agent_id = ?""",
            (mood, _now_ms(), tenant_id, agent_id),
        )
        await self.conn.commit()

    async def update_fatigue(
        self,
        agent_id: str,
        delta: float,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> None:
        """Add ``delta`` to fatigue, clamped to ``[0.0, 1.0]``.

        Negative deltas recover energy; positive ones consume it. The
        clamp lives in SQL so concurrent writers can't race the bounds.
        """
        await self.conn.execute(
            """UPDATE agent_persona_state
               SET fatigue = MAX(0.0, MIN(1.0, fatigue + ?)),
                   updated_at = ?
               WHERE tenant_id = ? AND agent_id = ?""",
            (float(delta), _now_ms(), tenant_id, agent_id),
        )
        await self.conn.commit()

    async def push_recent_topic(
        self,
        agent_id: str,
        topic: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> None:
        """Append ``topic`` to ``recent_topics`` with dedup + cap.

        Reads the current list, applies the same invariant the dataclass
        promises (last-occurrence wins, max :data:`RECENT_TOPICS_CAP`
        entries), and writes it back. Two-statement transaction kept short
        so the row-level lock window stays tiny.
        """
        current = await self.get(agent_id, tenant_id=tenant_id)
        if current is None:
            return
        new_topics = _dedup_cap([*current.recent_topics, topic])
        await self.conn.execute(
            """UPDATE agent_persona_state
               SET recent_topics = ?, updated_at = ?
               WHERE tenant_id = ? AND agent_id = ?""",
            (json.dumps(new_topics), _now_ms(), tenant_id, agent_id),
        )
        await self.conn.commit()

    async def list_all(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> list[PersonaState]:
        """Return every row in ``tenant_id``, sorted by agent_id for
        deterministic output."""
        cursor = await self.conn.execute(
            """SELECT agent_id, mood, fatigue, recent_topics,
                      updated_at, state_json
               FROM agent_persona_state
               WHERE tenant_id = ?
               ORDER BY agent_id ASC""",
            (tenant_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_state(r) for r in rows]

    async def delete(
        self,
        agent_id: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> bool:
        """Remove the row for ``(tenant_id, agent_id)``. Returns True if
        a row was deleted.

        Used by the ``reset`` CLI subcommand (operator action — the next
        seeder pass re-creates the row from the YAML defaults).
        """
        cursor = await self.conn.execute(
            "DELETE FROM agent_persona_state WHERE tenant_id = ? AND agent_id = ?",
            (tenant_id, agent_id),
        )
        await self.conn.commit()
        deleted = cursor.rowcount > 0
        await cursor.close()
        return deleted


__all__ = ["DEFAULT_TENANT_ID", "SCHEMA_SQL", "PersonaStore"]
