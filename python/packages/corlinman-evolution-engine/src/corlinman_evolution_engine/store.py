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


DEFAULT_TENANT_ID = "default"
"""Sentinel for legacy single-tenant deployments.

Phase 4 W1 4-1A added ``tenant_id`` to ``evolution_signals`` and
``evolution_proposals`` as a NOT NULL column with this value as the
default. Pre-4-1A test fixtures and any signal emitter that hasn't been
upgraded yet still produce rows without the column; the store
gracefully degrades to this constant in that case so the engine doesn't
need branching code paths.
"""


@dataclass(frozen=True)
class SignalRow:
    """One row from ``evolution_signals``.

    ``tenant_id`` defaults to ``"default"`` for legacy rows from a DB
    that pre-dates the Phase 4 W1 4-1A migration. The new Phase 4 W1
    4-1D handlers (``prompt_template`` / ``tool_policy``) propagate
    this value onto the proposals they emit so a multi-tenant
    deployment doesn't accidentally cross-pollinate.
    """

    id: int
    event_kind: str
    target: str | None
    severity: str
    payload: dict[str, Any]
    trace_id: str | None
    session_id: str | None
    observed_at: int  # unix ms
    tenant_id: str = DEFAULT_TENANT_ID


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
        # Populated by ``__aenter__`` once the connection is open. False
        # whenever the deployment is still on the pre-4-1A schema.
        self._signals_has_tenant: bool = False
        self._proposals_has_tenant: bool = False

    async def __aenter__(self) -> EvolutionStore:
        self._conn = await aiosqlite.connect(self._path)
        # Foreign keys are off by default in SQLite; the proposals table has
        # an FK to itself (rollback_of) so leave them on for safety.
        await self._conn.execute("PRAGMA foreign_keys = ON")
        # Probe whether the Phase 4 W1 4-1A ``tenant_id`` column has
        # landed yet on each table. Older snapshot DBs (and the
        # in-memory schemas used by some tests pre-4-1A) lack it; we
        # fall back to ``DEFAULT_TENANT_ID`` on those paths so the
        # engine + handlers stay schema-agnostic.
        self._signals_has_tenant = await _column_present(
            self._conn, "evolution_signals", "tenant_id"
        )
        self._proposals_has_tenant = await _column_present(
            self._conn, "evolution_proposals", "tenant_id"
        )
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
        """Read all signals with ``observed_at >= since_ms`` ordered ASC.

        Includes ``tenant_id`` when the Phase 4 W1 4-1A column is
        present; otherwise every row materialises with the legacy
        ``DEFAULT_TENANT_ID`` sentinel. The branch keeps Phase 2 / 3
        deployments and pre-migration test fixtures working unchanged
        while still letting Phase 4 callers (the new ``prompt_template``
        / ``tool_policy`` handlers) propagate non-default tenants when
        the column is there.
        """
        if self._signals_has_tenant:
            cursor = await self.conn.execute(
                """SELECT id, event_kind, target, severity, payload_json,
                          trace_id, session_id, observed_at, tenant_id
                   FROM evolution_signals
                   WHERE observed_at >= ?
                   ORDER BY observed_at ASC
                   LIMIT ?""",
                (since_ms, limit),
            )
        else:
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
        return [_decode_signal(r, has_tenant=self._signals_has_tenant) for r in rows]

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
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> None:
        """Append a fresh ``pending`` proposal.

        Includes ``tenant_id`` in the INSERT only when the Phase 4 W1
        4-1A column is present. On older schemas the value is dropped
        silently — the Rust applier is the source of truth for that
        migration and we don't want to fail an engine run because the
        operator hasn't yet bumped the gateway.
        """
        if self._proposals_has_tenant:
            await self.conn.execute(
                """INSERT INTO evolution_proposals
                     (id, kind, target, diff, reasoning, risk, budget_cost, status,
                      shadow_metrics, signal_ids, trace_ids,
                      created_at, decided_at, decided_by, applied_at, rollback_of,
                      tenant_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending',
                           NULL, ?, ?,
                           ?, NULL, NULL, NULL, NULL,
                           ?)""",
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
                    tenant_id,
                ),
            )
        else:
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

    async def existing_targets_for_kind(
        self, kind: str
    ) -> set[tuple[str, str]]:
        """``(target, tenant_id)`` pairs already proposed for ``kind``.

        Centralised here so every ``KindHandler`` shares the same dedup
        SQL — the handlers stay free of schema-detection branching. On
        pre-Phase-4-W1-4-1A schemas (no ``tenant_id`` column) every
        returned pair has ``tenant_id="default"``, so single-tenant
        deployments dedup identically to Phase 2 / 3.
        """
        if self._proposals_has_tenant:
            cursor = await self.conn.execute(
                "SELECT target, tenant_id FROM evolution_proposals "
                "WHERE kind = ?",
                (kind,),
            )
        else:
            cursor = await self.conn.execute(
                "SELECT target FROM evolution_proposals WHERE kind = ?",
                (kind,),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        if self._proposals_has_tenant:
            return {(str(r[0]), str(r[1] or DEFAULT_TENANT_ID)) for r in rows}
        return {(str(r[0]), DEFAULT_TENANT_ID) for r in rows}

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

def _decode_signal(
    row: aiosqlite.Row | tuple[Any, ...],
    *,
    has_tenant: bool = False,
) -> SignalRow:
    payload_str = row[4]
    try:
        payload = json.loads(payload_str) if payload_str else {}
    except json.JSONDecodeError:
        payload = {"_raw": payload_str}
    if has_tenant:
        raw_tenant = row[8] if len(row) > 8 else None
        tenant_id = str(raw_tenant) if raw_tenant else DEFAULT_TENANT_ID
    else:
        tenant_id = DEFAULT_TENANT_ID
    return SignalRow(
        id=int(row[0]),
        event_kind=str(row[1]),
        target=row[2],
        severity=str(row[3]),
        payload=payload,
        trace_id=row[5],
        session_id=row[6],
        observed_at=int(row[7]),
        tenant_id=tenant_id,
    )


async def _column_present(
    conn: aiosqlite.Connection, table: str, column: str
) -> bool:
    """``True`` when ``table.column`` exists in the live schema.

    Used by ``EvolutionStore.__aenter__`` to decide whether the Phase
    4 W1 4-1A ``tenant_id`` column has landed yet. We probe via
    ``PRAGMA table_info`` instead of catching INSERT failures so the
    decision is made once at connection-open time and every subsequent
    query knows which dialect to use.
    """
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    await cursor.close()
    return any(str(r[1]) == column for r in rows)


async def fetch_existing_targets(
    conn: aiosqlite.Connection, kind: str
) -> set[tuple[str, str]]:
    """``(target, tenant_id)`` pairs already proposed for ``kind``.

    Free function so any ``KindHandler`` can call it without dragging
    the ``EvolutionStore`` async context manager into its own state —
    the engine passes the raw connection to ``existing_targets`` per
    the protocol. Probes for the Phase 4 W1 4-1A ``tenant_id`` column
    on each call; the pragma is a memory-only lookup against SQLite's
    schema cache so the cost is in the noise compared to the actual
    SELECT.
    """
    has_tenant = await _column_present(conn, "evolution_proposals", "tenant_id")
    if has_tenant:
        cursor = await conn.execute(
            "SELECT target, tenant_id FROM evolution_proposals WHERE kind = ?",
            (kind,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {(str(r[0]), str(r[1] or DEFAULT_TENANT_ID)) for r in rows}
    cursor = await conn.execute(
        "SELECT target FROM evolution_proposals WHERE kind = ?",
        (kind,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return {(str(r[0]), DEFAULT_TENANT_ID) for r in rows}


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
