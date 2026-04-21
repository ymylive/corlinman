"""Read-only client for the gateway's SQLite session store.

Sprint 9 T3 primitive — *not* DeepMemo (S16). This module lets Python code
list past chat turns keyed by ``(session_key, [time-range])`` so that:

* S12 sub-agent orchestration (``AgentAssistant``) can pull recent context
  from a parent agent's session before delegating.
* S16 DeepMemo can range-scan a session for turns to summarise.

Transport choice (explained so reviewers don't wonder):

    The Rust gateway persists sessions in ``<data_dir>/sessions.sqlite``
    (single file, WAL journal) via ``corlinman_core::session_sqlite``.
    Every row has a stable ``(session_key, seq)`` primary key plus an
    RFC3339 ``ts`` column. A read-only Python client can open the same
    file directly with :mod:`sqlite3` — the alternative (new admin gRPC
    RPC or a new HTTP endpoint on the Rust side) would need a proto
    regen + auth plumbing, which this primitive doesn't justify. Python
    opens the DB with ``mode=ro`` via a URI so we cannot accidentally
    mutate gateway state, and the schema is stable under ``CREATE TABLE
    IF NOT EXISTS`` so a running gateway never blocks our reads.

    If future corlinman deployments separate the Rust gateway and Python
    plane across machines, this helper grows a ``transport=`` parameter
    and a thin gRPC client — the pydantic shape stays the same.

Interface:

* :class:`SessionMessage` — pydantic model (role, content, timestamp,
  session_key, tool_call_id, tool_calls).
* :class:`SessionQueryClient` — thin wrapper around the SQLite file with
  ``list_messages(session_key, limit=..., before=..., after=...)``.
* :class:`SessionQueryError` — raised for schema / IO failures. Callers
  decide whether to propagate or degrade.

Tests inject a seeded in-memory / tmp-file SQLite database to mock the
gateway store end-to-end without ever touching a real transport.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "SessionMessage",
    "SessionQueryClient",
    "SessionQueryError",
    "SessionRole",
]


SessionRole = Literal["system", "user", "assistant", "tool"]


class SessionQueryError(RuntimeError):
    """Raised when the underlying SQLite store can't be read.

    The caller (S12 orchestrator, S16 DeepMemo) decides whether to
    surface this to the user or degrade to an empty history. We never
    swallow the error here.
    """


class SessionMessage(BaseModel):
    """Pydantic projection of one row from the Rust ``sessions`` table.

    Fields mirror ``corlinman_core::session::SessionMessage`` — see
    ``rust/crates/corlinman-core/src/session_sqlite.rs`` for the
    authoritative schema.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_key: str = Field(..., description="Stable conversation identifier.")
    seq: int = Field(..., ge=0, description="Per-session monotonic sequence.")
    role: SessionRole
    content: str
    ts: datetime
    tool_call_id: str | None = None
    tool_calls: list[dict[str, object]] | None = None


class SessionQueryClient:
    """Read-only client for ``sessions.sqlite``.

    Cheap to instantiate (no open handle held between calls): every
    query opens a fresh read-only connection via the
    ``file:…?mode=ro`` URI, runs, and closes. That matches the
    expected usage pattern (occasional context fetches during prompt
    composition) and keeps us from fighting the gateway's WAL writer.
    """

    def __init__(self, sqlite_path: str | Path) -> None:
        self._path = Path(sqlite_path)

    @property
    def path(self) -> Path:
        return self._path

    def list_messages(
        self,
        session_key: str,
        *,
        limit: int | None = None,
        before: datetime | None = None,
        after: datetime | None = None,
    ) -> list[SessionMessage]:
        """Return stored messages for ``session_key``, chronologically ascending.

        Args:
            session_key: stable conversation identifier.
            limit: cap on returned rows. ``None`` → no cap. When both
                ``limit`` and ``before`` are given, we keep the newest
                ``limit`` rows strictly before the cutoff (matches
                S12's "give me the last N turns before X" need).
            before: drop rows whose ``ts >= before`` (exclusive upper
                bound, so callers can thread paging by feeding the
                previous batch's earliest ts).
            after: drop rows whose ``ts <= after`` (exclusive lower
                bound).

        Raises:
            SessionQueryError: when the SQLite file is missing / the
            schema doesn't match / the query fails. The exception
            preserves the sqlite3 message so operators can diagnose.
        """
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")

        clauses: list[str] = ["session_key = :session_key"]
        params: dict[str, object] = {"session_key": session_key}
        if before is not None:
            clauses.append("ts < :before")
            params["before"] = _rfc3339(before)
        if after is not None:
            clauses.append("ts > :after")
            params["after"] = _rfc3339(after)
        where_sql = " AND ".join(clauses)

        # When we have a limit AND a before cutoff, we want the newest
        # N rows, then re-sort ascending on the way out so callers
        # always see time-ordered history.
        order = "DESC" if limit is not None and before is not None else "ASC"
        limit_sql = f" LIMIT {int(limit)}" if limit is not None else ""
        sql = (
            "SELECT session_key, seq, role, content, tool_call_id, tool_calls_json, ts "
            f"FROM sessions WHERE {where_sql} ORDER BY seq {order}{limit_sql}"
        )

        try:
            with closing(self._connect()) as conn:
                cur = conn.execute(sql, params)
                rows = cur.fetchall()
        except sqlite3.Error as exc:
            raise SessionQueryError(
                f"session query failed on {self._path}: {exc}"
            ) from exc

        messages = [self._row_to_message(r) for r in rows]
        if order == "DESC":
            messages.reverse()
        return messages

    # ------------------------------------------------------------------ impl

    def _connect(self) -> sqlite3.Connection:
        # URI form with `mode=ro` guarantees we never lock the file
        # for writing and fails fast if the file is missing (instead of
        # silently creating an empty one, which sqlite3 would otherwise
        # do with a plain path).
        if not self._path.exists():
            raise SessionQueryError(f"sessions sqlite not found: {self._path}")
        uri = f"file:{self._path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> SessionMessage:
        tool_calls_raw = row["tool_calls_json"]
        tool_calls: list[dict[str, object]] | None = None
        if tool_calls_raw:
            import json

            try:
                decoded = json.loads(tool_calls_raw)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, list):
                tool_calls = [d for d in decoded if isinstance(d, dict)]

        return SessionMessage(
            session_key=row["session_key"],
            seq=int(row["seq"]),
            role=row["role"],
            content=row["content"],
            ts=_parse_rfc3339(row["ts"]),
            tool_call_id=row["tool_call_id"],
            tool_calls=tool_calls,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _rfc3339(dt: datetime) -> str:
    """Serialise a datetime the way the Rust side writes ``ts``.

    The Rust layer uses ``time::OffsetDateTime::format(Rfc3339)`` which
    emits ``...T...+00:00`` (or ``Z`` for UTC). We always store UTC so
    Python converts naive inputs to UTC before formatting. This matches
    the Rust comparison operator's lexicographic ordering as long as
    inputs are in UTC — which they will be because Python callers go
    through this helper.
    """
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    # isoformat gives us e.g. `2026-04-21T09:30:00+00:00`; strip the
    # `+00:00` → `Z` suffix so it's byte-for-byte what the Rust side
    # writes when the OffsetDateTime is at offset 0.
    iso = dt.isoformat()
    return iso.replace("+00:00", "Z")


def _parse_rfc3339(raw: str) -> datetime:
    """Parse an RFC3339 timestamp produced by the Rust session store."""
    # `Z` → `+00:00` so `datetime.fromisoformat` accepts it. Python 3.11+
    # handles `Z` natively, but this stays explicit for readers.
    normalised = raw.replace("Z", "+00:00")
    return datetime.fromisoformat(normalised)
