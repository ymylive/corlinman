"""SQLite-backed session store — Python port.

Mirrors ``rust/crates/corlinman-core/src/session.rs`` plus
``rust/crates/corlinman-core/src/session_sqlite.rs`` enough to power
deterministic replay. The replay primitive only ever reads (``load`` +
``list_sessions``), but ``append`` is also ported so tests can seed
fixtures without falling back to raw SQL — same pattern the Rust
``replay`` tests use.

Schema (single table, one row per message):

.. code-block:: sql

    CREATE TABLE IF NOT EXISTS sessions (
        session_key TEXT NOT NULL,
        seq INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        tool_call_id TEXT,
        tool_calls_json TEXT,
        ts TEXT NOT NULL,
        tenant_id TEXT NOT NULL DEFAULT 'default',
        PRIMARY KEY (session_key, seq)
    );

``ts`` is RFC-3339 text. ``tool_calls_json`` is the OpenAI tool_calls
array verbatim as a JSON string.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import aiosqlite


class CorlinmanError(RuntimeError):
    """Storage-layer error. Mirrors ``corlinman_core::CorlinmanError::Storage``
    — collapsed to a single Python exception so callers can ``except`` once."""


def _storage(op: str, exc: BaseException) -> CorlinmanError:
    """Wrap a sqlite error into :class:`CorlinmanError` with a short
    operation tag so logs can distinguish failing queries."""
    return CorlinmanError(f"sessions {op}: {exc}")


# Full DDL applied on open. Idempotent — safe against an existing file.
# Carries the Phase 4 W1 ``tenant_id`` column on fresh DBs; legacy DBs
# without it get the column added by :func:`_ensure_tenant_column`.
SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS sessions (
    session_key TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_call_id TEXT,
    tool_calls_json TEXT,
    ts TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    PRIMARY KEY (session_key, seq)
);
CREATE INDEX IF NOT EXISTS idx_sessions_key ON sessions(session_key);
"""

# Index that references the Phase 4 W1 ``tenant_id`` column. Run after
# :func:`_ensure_tenant_column` so the column exists on legacy DBs
# before SQLite resolves the index column names.
_TENANT_INDEX_SQL: str = """
CREATE INDEX IF NOT EXISTS idx_sessions_tenant_key
    ON sessions(tenant_id, session_key, seq);
"""


class SessionRole(str, Enum):
    """Role of a persisted message. Matches the OpenAI chat roles with a
    dedicated ``TOOL`` variant so tool responses round-trip cleanly."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"

    def as_str(self) -> str:
        return self.value

    @classmethod
    def from_str(cls, value: str) -> SessionRole:
        """Inverse of :meth:`as_str`. Unknown strings fall back to
        ``USER`` — matches the Rust ``SessionRole::from_str`` impl."""
        if value == "assistant":
            return cls.ASSISTANT
        if value == "system":
            return cls.SYSTEM
        if value == "tool":
            return cls.TOOL
        return cls.USER


@dataclass(slots=True)
class SessionMessage:
    """One persisted message. ``ts`` is timezone-aware UTC. Mirrors
    ``corlinman_core::SessionMessage`` 1:1."""

    role: SessionRole
    content: str
    ts: datetime
    tool_call_id: str | None = None
    tool_calls: Any | None = None  # JSON value (dict / list / scalar)

    @classmethod
    def user(cls, content: str) -> SessionMessage:
        """Convenience constructor for a user message with ``ts = now()``."""
        return cls(
            role=SessionRole.USER,
            content=content,
            tool_call_id=None,
            tool_calls=None,
            ts=datetime.now(timezone.utc),
        )

    @classmethod
    def assistant(cls, content: str, tool_calls: Any | None = None) -> SessionMessage:
        """Convenience constructor for an assistant message with ``ts = now()``."""
        return cls(
            role=SessionRole.ASSISTANT,
            content=content,
            tool_call_id=None,
            tool_calls=tool_calls,
            ts=datetime.now(timezone.utc),
        )


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """One row returned by :meth:`SqliteSessionStore.list_sessions`.

    Used by the admin sessions list route so the operator UI can paint
    a roster without loading every transcript. ``last_message_at_ms``
    is unix milliseconds — picked over RFC-3339 to keep the wire shape
    numeric (sortable + cheap to compare).
    """

    session_key: str
    last_message_at_ms: int
    message_count: int


# RFC-3339 / ISO-8601 format spec used by the store. Matches the Rust
# ``time::format_description::well_known::Rfc3339`` output: e.g.
# ``"2026-04-30T12:34:56.789Z"`` -- ``isoformat`` with a ``Z`` swap for
# the ``+00:00`` suffix gives the same shape Rust writes.
def _format_rfc3339(ts: datetime) -> str:
    """Format ``ts`` as RFC-3339 / ISO-8601 text, normalised to UTC."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    # Python's ``isoformat`` emits ``+00:00`` for UTC; canonicalise to ``Z``
    # so the wire shape matches the Rust ``Rfc3339`` formatter.
    s = ts.isoformat()
    if s.endswith("+00:00"):
        s = s[: -len("+00:00")] + "Z"
    return s


def _parse_rfc3339(raw: str) -> datetime:
    """Parse an RFC-3339 / ISO-8601 timestamp into a timezone-aware
    :class:`datetime`. Accepts both ``Z`` and ``+00:00`` suffixes."""
    # ``fromisoformat`` accepts ``Z`` from Python 3.11+ but we
    # normalise here to be explicit about UTC semantics on older inputs.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw)


class SqliteSessionStore:
    """Async SQLite-backed session store.

    Use as an async context manager (``async with``) or call
    :meth:`open` directly and remember to ``await store.close()``. The
    underlying ``aiosqlite.Connection`` is held privately so the store
    owns transaction boundaries.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @classmethod
    async def open(cls, path: Path) -> SqliteSessionStore:
        """Open (or create) the sessions database at ``path``.

        Opens with WAL + ``synchronous=NORMAL`` for write throughput,
        applies :data:`SCHEMA_SQL`, then idempotently adds the Phase 4
        W1 ``tenant_id`` column on legacy DBs.
        """
        store = cls(path)
        await store._open()
        return store

    async def __aenter__(self) -> SqliteSessionStore:
        await self._open()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def _open(self) -> None:
        try:
            conn = await aiosqlite.connect(self._path)
        except Exception as exc:  # pragma: no cover - aiosqlite raises sqlite3.Error
            raise _storage("connect", exc) from exc
        try:
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.execute("PRAGMA synchronous = NORMAL")
            await conn.execute("PRAGMA busy_timeout = 5000")

            try:
                await conn.executescript(SCHEMA_SQL)
            except Exception as exc:
                raise _storage("apply_schema", exc) from exc

            # Phase 4 W1: idempotent tenant_id column add for legacy
            # pre-tenant DBs. Probe via pragma_table_info; on miss,
            # ALTER adds the column with ``NOT NULL DEFAULT 'default'``
            # so legacy rows backfill at ALTER time.
            await self._ensure_tenant_column(conn)

            try:
                await conn.executescript(_TENANT_INDEX_SQL)
            except Exception as exc:
                raise _storage("apply_tenant_index", exc) from exc

            await conn.commit()
        except CorlinmanError:
            await conn.close()
            raise
        except Exception as exc:
            await conn.close()
            raise _storage("open", exc) from exc

        self._conn = conn

    @staticmethod
    async def _ensure_tenant_column(conn: aiosqlite.Connection) -> None:
        try:
            cursor = await conn.execute(
                "SELECT 1 FROM pragma_table_info('sessions') WHERE name = ?",
                ("tenant_id",),
            )
            row = await cursor.fetchone()
            await cursor.close()
        except Exception as exc:
            raise _storage("probe_tenant", exc) from exc
        if row is None:
            try:
                await conn.execute(
                    "ALTER TABLE sessions ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'"
                )
            except Exception as exc:
                raise _storage("alter_tenant", exc) from exc

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _c(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError(
                "SqliteSessionStore used outside async context (call open() first)"
            )
        return self._conn

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def load(self, session_key: str) -> list[SessionMessage]:
        """Load full history for a session, ordered by ``seq`` ASC.

        Returns an empty list when the session does not exist.
        """
        try:
            cursor = await self._c.execute(
                "SELECT role, content, tool_call_id, tool_calls_json, ts "
                "FROM sessions WHERE session_key = ? ORDER BY seq ASC",
                (session_key,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        except Exception as exc:
            raise _storage("load", exc) from exc

        out: list[SessionMessage] = []
        for r in rows:
            role_raw: str = r[0]
            content: str = r[1]
            tool_call_id: str | None = r[2]
            tool_calls_json: str | None = r[3]
            ts_raw: str = r[4]
            try:
                ts = _parse_rfc3339(ts_raw)
            except ValueError:
                # Mirror the Rust ``unwrap_or_else(now_utc)`` fallback —
                # corrupted ts falls back to ``now()`` rather than
                # failing the whole load.
                ts = datetime.now(timezone.utc)
            tool_calls: Any | None = None
            if tool_calls_json is not None:
                try:
                    tool_calls = json.loads(tool_calls_json)
                except ValueError:
                    tool_calls = None
            out.append(
                SessionMessage(
                    role=SessionRole.from_str(role_raw),
                    content=content,
                    tool_call_id=tool_call_id,
                    tool_calls=tool_calls,
                    ts=ts,
                )
            )
        return out

    async def iter_messages(self, session_key: str) -> AsyncIterator[SessionMessage]:
        """Async-iterate messages for ``session_key`` in ``seq`` order.

        Streams from SQLite one row at a time rather than buffering the
        whole transcript in memory. Useful for long sessions where the
        replay caller only wants to fold once over the messages.
        """
        try:
            cursor = await self._c.execute(
                "SELECT role, content, tool_call_id, tool_calls_json, ts "
                "FROM sessions WHERE session_key = ? ORDER BY seq ASC",
                (session_key,),
            )
        except Exception as exc:
            raise _storage("iter", exc) from exc
        try:
            async for r in cursor:
                role_raw: str = r[0]
                content: str = r[1]
                tool_call_id: str | None = r[2]
                tool_calls_json: str | None = r[3]
                ts_raw: str = r[4]
                try:
                    ts = _parse_rfc3339(ts_raw)
                except ValueError:
                    ts = datetime.now(timezone.utc)
                tool_calls: Any | None = None
                if tool_calls_json is not None:
                    try:
                        tool_calls = json.loads(tool_calls_json)
                    except ValueError:
                        tool_calls = None
                yield SessionMessage(
                    role=SessionRole.from_str(role_raw),
                    content=content,
                    tool_call_id=tool_call_id,
                    tool_calls=tool_calls,
                    ts=ts,
                )
        finally:
            await cursor.close()

    async def list_sessions(self) -> list[SessionSummary]:
        """Aggregate per-session metadata for the admin sessions list.

        Returns one :class:`SessionSummary` per distinct ``session_key``,
        ordered by ``MAX(ts) DESC`` so the most-recent session shows up
        first without a follow-up sort.
        """
        try:
            cursor = await self._c.execute(
                "SELECT session_key, MAX(ts) AS last_ts, COUNT(*) AS msg_count "
                "FROM sessions "
                "GROUP BY session_key "
                "ORDER BY MAX(ts) DESC"
            )
            rows = await cursor.fetchall()
            await cursor.close()
        except Exception as exc:
            raise _storage("list_sessions", exc) from exc

        out: list[SessionSummary] = []
        for row in rows:
            session_key: str = row[0]
            last_ts: str = row[1]
            msg_count: int = int(row[2])
            try:
                dt = _parse_rfc3339(last_ts)
            except ValueError:
                # Mirror the Rust warn+skip behaviour on unparseable ts.
                continue
            last_ms = int(dt.timestamp() * 1000)
            out.append(
                SessionSummary(
                    session_key=session_key,
                    last_message_at_ms=last_ms,
                    message_count=msg_count,
                )
            )
        return out

    # ------------------------------------------------------------------
    # Writes (test-fixture helpers; replay itself is read-only)
    # ------------------------------------------------------------------

    async def append(self, session_key: str, message: SessionMessage) -> None:
        """Append one message to the session. The store assigns the next ``seq``.

        Computes next ``seq`` under a ``BEGIN IMMEDIATE`` transaction so
        two concurrent appends to the same key cannot both observe the
        same ``MAX(seq)``.
        """
        conn = self._c
        try:
            await conn.execute("BEGIN IMMEDIATE")
        except Exception as exc:
            raise _storage("begin_immediate", exc) from exc

        try:
            try:
                cursor = await conn.execute(
                    "SELECT COALESCE(MAX(seq), -1) + 1 FROM sessions WHERE session_key = ?",
                    (session_key,),
                )
                row = await cursor.fetchone()
                await cursor.close()
            except Exception as exc:
                raise _storage("next_seq", exc) from exc
            next_seq = int(row[0]) if row is not None else 0

            ts_str = _format_rfc3339(message.ts)
            tool_calls_text: str | None = None
            if message.tool_calls is not None:
                try:
                    tool_calls_text = json.dumps(message.tool_calls)
                except (TypeError, ValueError) as exc:
                    raise _storage("serialize_tool_calls", exc) from exc

            try:
                await conn.execute(
                    "INSERT INTO sessions("
                    "session_key, seq, role, content, tool_call_id, tool_calls_json, ts"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_key,
                        next_seq,
                        message.role.as_str(),
                        message.content,
                        message.tool_call_id,
                        tool_calls_text,
                        ts_str,
                    ),
                )
            except Exception as exc:
                raise _storage("insert", exc) from exc

            await conn.commit()
        except BaseException:
            try:
                await conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001 - rollback failures are non-fatal here
                pass
            raise

    async def delete(self, session_key: str) -> None:
        """Delete every message for the session. No-op when missing."""
        try:
            await self._c.execute(
                "DELETE FROM sessions WHERE session_key = ?", (session_key,)
            )
            await self._c.commit()
        except Exception as exc:
            raise _storage("delete", exc) from exc


__all__ = [
    "CorlinmanError",
    "SCHEMA_SQL",
    "SessionMessage",
    "SessionRole",
    "SessionSummary",
    "SqliteSessionStore",
]
