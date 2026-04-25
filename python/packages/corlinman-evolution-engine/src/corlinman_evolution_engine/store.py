"""SQLite access for the evolution + kb databases.

Read-mostly. The Rust crate ``corlinman-evolution`` owns the
``evolution.sqlite`` schema; we only consume ``evolution_signals`` and
append to ``evolution_proposals``. ``kb.sqlite`` is owned by
``corlinman-vector`` and we never write to it.

All times are unix milliseconds (i64) to match the Rust types.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

# ---------------------------------------------------------------------------
# Row dataclasses — narrow projections of the underlying tables, only the
# columns the Phase 2 engine actually reads.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalRow:
    """One row from ``evolution_signals``."""

    id: int
    event_kind: str
    target: str | None
    severity: str
    payload: dict[str, Any]
    trace_id: str | None
    session_id: str | None
    observed_at: int  # unix ms


@dataclass(frozen=True)
class ChunkRow:
    """A chunk we may consider for ``memory_op`` proposals."""

    id: int
    namespace: str
    content: str


# ---------------------------------------------------------------------------
# Evolution DB
# ---------------------------------------------------------------------------


class EvolutionStore:
    """Async wrapper around ``evolution.sqlite``.

    Use as an async context manager so connections close cleanly even if a
    run aborts mid-way.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> EvolutionStore:
        self._conn = await aiosqlite.connect(self._path)
        # Foreign keys are off by default in SQLite; the proposals table has
        # an FK to itself (rollback_of) so leave them on for safety.
        await self._conn.execute("PRAGMA foreign_keys = ON")
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("EvolutionStore used outside async context")
        return self._conn

    async def list_signals_since(
        self,
        since_ms: int,
        limit: int = 10_000,
    ) -> list[SignalRow]:
        """Read all signals with ``observed_at >= since_ms`` ordered ASC."""
        cursor = await self.conn.execute(
            """SELECT id, event_kind, target, severity, payload_json,
                      trace_id, session_id, observed_at
               FROM evolution_signals
               WHERE observed_at >= ?
               ORDER BY observed_at ASC
               LIMIT ?""",
            (since_ms, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_decode_signal(r) for r in rows]

    async def insert_proposal(
        self,
        *,
        proposal_id: str,
        kind: str,
        target: str,
        diff: str,
        reasoning: str,
        risk: str,
        budget_cost: int,
        signal_ids: list[int],
        trace_ids: list[str],
        created_at: int,
    ) -> None:
        """Append a fresh ``pending`` proposal."""
        await self.conn.execute(
            """INSERT INTO evolution_proposals
                 (id, kind, target, diff, reasoning, risk, budget_cost, status,
                  shadow_metrics, signal_ids, trace_ids,
                  created_at, decided_at, decided_by, applied_at, rollback_of)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending',
                       NULL, ?, ?,
                       ?, NULL, NULL, NULL, NULL)""",
            (
                proposal_id,
                kind,
                target,
                diff,
                reasoning,
                risk,
                budget_cost,
                json.dumps(signal_ids),
                json.dumps(trace_ids),
                created_at,
            ),
        )
        await self.conn.commit()

    async def count_proposals_on_day(self, day_prefix: str) -> int:
        """Count rows whose id starts with ``day_prefix`` (e.g. ``evol-2026-04-25``).

        Used to mint the next 3-digit sequence number for the daily id.
        """
        cursor = await self.conn.execute(
            "SELECT COUNT(*) FROM evolution_proposals WHERE id LIKE ?",
            (f"{day_prefix}%",),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0]) if row is not None else 0

def _decode_signal(row: aiosqlite.Row | tuple[Any, ...]) -> SignalRow:
    payload_str = row[4]
    try:
        payload = json.loads(payload_str) if payload_str else {}
    except json.JSONDecodeError:
        payload = {"_raw": payload_str}
    return SignalRow(
        id=int(row[0]),
        event_kind=str(row[1]),
        target=row[2],
        severity=str(row[3]),
        payload=payload,
        trace_id=row[5],
        session_id=row[6],
        observed_at=int(row[7]),
    )


# ---------------------------------------------------------------------------
# KB DB (corlinman-vector's kb.sqlite)
# ---------------------------------------------------------------------------


class KbStore:
    """Read-only async wrapper around ``kb.sqlite``.

    We only need the ``chunks`` table for near-duplicate detection. Vectors
    are skipped on purpose — Phase 2 is content-only Jaccard.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> KbStore:
        # ``mode=ro`` on the URI keeps us honest about read-only access.
        uri = f"file:{self._path}?mode=ro"
        self._conn = await aiosqlite.connect(uri, uri=True)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("KbStore used outside async context")
        return self._conn

    async def list_chunks(
        self,
        *,
        namespace: str | None = None,
        limit: int = 5_000,
    ) -> list[ChunkRow]:
        """Return chunks ordered by id. Optionally filter by namespace."""
        if namespace is None:
            cursor = await self.conn.execute(
                "SELECT id, namespace, content FROM chunks ORDER BY id ASC LIMIT ?",
                (limit,),
            )
        else:
            cursor = await self.conn.execute(
                """SELECT id, namespace, content
                   FROM chunks
                   WHERE namespace = ?
                   ORDER BY id ASC
                   LIMIT ?""",
                (namespace, limit),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            ChunkRow(id=int(r[0]), namespace=str(r[1]), content=str(r[2])) for r in rows
        ]
