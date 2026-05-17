"""Shared blackboard for sibling subagents (v0.7 multi-agent release).

The orchestrator persona dispatches multiple sibling children via
``subagent.spawn_many``; those siblings often need to coordinate by
reading and writing a shared scratchpad rather than passing JSON back
through the parent. The blackboard is that scratchpad: a small, typed,
trace-scoped key-value store backed by SQLite.

Design constraints baked in here:

1. **Trace-scoped.** Every read / write is keyed by ``(trace_id, key)``.
   A child cannot reach into another trace's data — the supervisor's
   :class:`ParentContext` already carries ``trace_id`` and the dispatch
   layer threads it in. The blackboard table itself stores ``trace_id``
   in plain text so forensic queries can join against the existing
   episodes / signals tables.
2. **Append-only writes, snapshot reads.** Writes never overwrite; each
   write inserts a new row with ``written_at``. A read returns the
   *latest* value at call time. This means concurrent siblings writing
   the same key never lose data — the later read sees whichever finished
   later, and history is preserved for replay.
3. **Self-creating schema.** The table is ``CREATE IF NOT EXISTS``-ed
   on each :class:`BlackboardStore` open. No migration ceremony — the
   blackboard is operational scratch, not durable user data. If the
   ops team wants to wipe it, ``DELETE FROM blackboard WHERE
   written_at < ?`` is enough.
4. **Wire shape mirrors subagent tool wrappers.** Both ``blackboard.read``
   and ``blackboard.write`` follow the same
   ``dispatch_<tool>(args_json=..., ...) -> str`` pattern the
   ``subagent.spawn`` family uses, so the gateway tool dispatcher
   routes them identically.

What this module deliberately does NOT do:

- No RBAC. Allowlisting which agents may read / write is the agent
  card's job (``tools_allowed`` lists ``blackboard.read`` /
  ``blackboard.write`` or it doesn't).
- No TTL. Operator-side janitor cron is the v0.8 conversation.
- No vector indexing. The blackboard is for short, structured
  hand-offs; for retrieval-heavy state use the KB.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


#: Wire-stable tool names. The orchestrator agent card and the gateway
#: dispatcher import these so the literal string lives in one place.
BLACKBOARD_READ_TOOL: str = "blackboard.read"
BLACKBOARD_WRITE_TOOL: str = "blackboard.write"

#: Sentinel error returned when ``args.key`` / ``args.value`` fail
#: validation. Same shape as the subagent dispatch errors so the LLM's
#: error-handling branch is uniform across the tool surface.
BLACKBOARD_ARGS_INVALID_ERROR: str = "args_invalid"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS blackboard (
    trace_id    TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    written_at  INTEGER NOT NULL,
    written_by  TEXT NOT NULL,
    PRIMARY KEY (trace_id, key, written_at)
);
CREATE INDEX IF NOT EXISTS idx_blackboard_trace_key
    ON blackboard (trace_id, key, written_at DESC);
"""


class BlackboardStore:
    """Thin sqlite wrapper. Construct once at gateway boot, share via
    reference; methods are short-lived and the connection is per-call so
    multiple coroutines can drive the store without a shared cursor.

    The store does *not* hold an open connection — sqlite3 is happy to
    reopen the file at request frequency, and per-call connections sidestep
    the "sqlite is not thread-safe" footgun without us reaching for
    aiosqlite. Latency on opens is < 1 ms for a warm filesystem.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._ensure_schema()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _ensure_schema(self) -> None:
        with self._open() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        # ``IMMEDIATE`` makes write transactions serialize without
        # SQLITE_BUSY surprises when two siblings hit the same row.
        # Readers are still concurrent under WAL.
        conn = sqlite3.connect(self._db_path, isolation_level="IMMEDIATE")
        try:
            yield conn
        finally:
            conn.close()

    def write(
        self,
        *,
        trace_id: str,
        key: str,
        value: str,
        written_by: str,
    ) -> int:
        """Append one (trace_id, key, value, written_at, written_by)
        row. Returns the ``written_at`` ms-since-epoch. The caller may
        use it as a write-receipt id for replay queries.
        """
        ts = _now_ms()
        with self._open() as conn:
            try:
                conn.execute(
                    "INSERT INTO blackboard "
                    "(trace_id, key, value, written_at, written_by) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (trace_id, key, value, ts, written_by),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                # Three or more rapid writes can land in the same
                # millisecond, exhausting the naive ``ts + 1`` retry.
                # Pick the smallest free ts above the existing max for
                # this (trace_id, key) — guarantees strictly increasing
                # ordering and avoids retry loops.
                row = conn.execute(
                    "SELECT COALESCE(MAX(written_at), ?) FROM blackboard "
                    "WHERE trace_id = ? AND key = ?",
                    (ts, trace_id, key),
                ).fetchone()
                ts = int(row[0]) + 1
                conn.execute(
                    "INSERT INTO blackboard "
                    "(trace_id, key, value, written_at, written_by) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (trace_id, key, value, ts, written_by),
                )
                conn.commit()
        return ts

    def read_latest(self, *, trace_id: str, key: str) -> str | None:
        """Return the most recent value for ``(trace_id, key)``, or
        ``None`` if no row exists. Snapshot-at-call semantics: the
        value seen here is whichever sibling wrote last *as of now*.
        """
        with self._open() as conn:
            row = conn.execute(
                "SELECT value FROM blackboard "
                "WHERE trace_id = ? AND key = ? "
                "ORDER BY written_at DESC LIMIT 1",
                (trace_id, key),
            ).fetchone()
            return row[0] if row else None


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------


def blackboard_read_tool_schema() -> dict[str, Any]:
    """OpenAI-shaped descriptor for ``blackboard.read``."""
    return {
        "type": "function",
        "function": {
            "name": BLACKBOARD_READ_TOOL,
            "description": (
                "Read the latest value for a shared key on the "
                "trace-scoped blackboard. Returns "
                "{\"key\": str, \"value\": str | null, \"present\": bool}. "
                "Use to coordinate with sibling agents dispatched via "
                "subagent.spawn_many."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": (
                            "Blackboard key to read. Shared across "
                            "siblings within the same trace."
                        ),
                    },
                },
                "required": ["key"],
                "additionalProperties": False,
            },
        },
    }


def blackboard_write_tool_schema() -> dict[str, Any]:
    """OpenAI-shaped descriptor for ``blackboard.write``."""
    return {
        "type": "function",
        "function": {
            "name": BLACKBOARD_WRITE_TOOL,
            "description": (
                "Append a value under a shared key on the trace-scoped "
                "blackboard. Writes never overwrite; each write is a "
                "new row. Reads return the latest value. Use to publish "
                "findings, partial results, or status to sibling agents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Blackboard key to write under.",
                    },
                    "value": {
                        "type": "string",
                        "description": (
                            "Value to publish. Stringify structured "
                            "data as JSON if siblings expect it."
                        ),
                    },
                },
                "required": ["key", "value"],
                "additionalProperties": False,
            },
        },
    }


# ---------------------------------------------------------------------------
# Dispatchers
# ---------------------------------------------------------------------------


def dispatch_blackboard_read(
    *,
    args_json: bytes | str,
    store: BlackboardStore,
    trace_id: str,
) -> str:
    """Translate one ``blackboard.read`` tool call into a JSON envelope.

    Returns a JSON string the gateway dispatcher feeds straight into
    ``ToolResult.content``. The envelope keys are
    ``{"key": str, "value": str | null, "present": bool}``; never
    raises (the parent's loop must keep going).
    """
    try:
        key = _parse_key(args_json)
    except _BlackboardArgsInvalidError as exc:
        return json.dumps(
            {"error": f"{BLACKBOARD_ARGS_INVALID_ERROR}: {exc.message}"}
        )
    value = store.read_latest(trace_id=trace_id, key=key)
    return json.dumps({"key": key, "value": value, "present": value is not None})


def dispatch_blackboard_write(
    *,
    args_json: bytes | str,
    store: BlackboardStore,
    trace_id: str,
    written_by: str,
) -> str:
    """Translate one ``blackboard.write`` tool call into a JSON envelope.

    Returns ``{"key": str, "written_at": int, "written_by": str}`` on
    success, or ``{"error": "..."}`` on args validation failure.
    """
    try:
        key, value = _parse_key_value(args_json)
    except _BlackboardArgsInvalidError as exc:
        return json.dumps(
            {"error": f"{BLACKBOARD_ARGS_INVALID_ERROR}: {exc.message}"}
        )
    written_at = store.write(
        trace_id=trace_id,
        key=key,
        value=value,
        written_by=written_by,
    )
    return json.dumps(
        {"key": key, "written_at": written_at, "written_by": written_by}
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


class _BlackboardArgsInvalidError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _decode(args_json: bytes | str) -> dict[str, Any]:
    if isinstance(args_json, (bytes, bytearray)):
        try:
            decoded = args_json.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _BlackboardArgsInvalidError(f"args_json not utf-8: {exc}") from exc
    else:
        decoded = args_json
    try:
        raw = json.loads(decoded) if decoded else {}
    except json.JSONDecodeError as exc:
        raise _BlackboardArgsInvalidError(f"args_json not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise _BlackboardArgsInvalidError(
            f"args_json must be a JSON object, got {type(raw).__name__}"
        )
    return raw


def _parse_key(args_json: bytes | str) -> str:
    raw = _decode(args_json)
    key = raw.get("key")
    if not isinstance(key, str) or not key:
        raise _BlackboardArgsInvalidError("missing or empty 'key' field")
    return key


def _parse_key_value(args_json: bytes | str) -> tuple[str, str]:
    raw = _decode(args_json)
    key = raw.get("key")
    if not isinstance(key, str) or not key:
        raise _BlackboardArgsInvalidError("missing or empty 'key' field")
    value = raw.get("value")
    if not isinstance(value, str):
        raise _BlackboardArgsInvalidError("'value' must be a string")
    return key, value


def _now_ms() -> int:
    """Wall-clock ms-since-epoch. Tests monkey-patch this for
    deterministic ordering."""
    return int(time.time() * 1000)


__all__ = [
    "BLACKBOARD_ARGS_INVALID_ERROR",
    "BLACKBOARD_READ_TOOL",
    "BLACKBOARD_WRITE_TOOL",
    "BlackboardStore",
    "blackboard_read_tool_schema",
    "blackboard_write_tool_schema",
    "dispatch_blackboard_read",
    "dispatch_blackboard_write",
]
