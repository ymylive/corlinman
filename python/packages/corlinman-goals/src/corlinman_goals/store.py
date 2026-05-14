"""Async SQLite store for ``agent_goals.sqlite``.

Mirrors the schema defined in ``docs/design/phase4-w4-d2-design.md``
§"Schema". The store owns the schema (no Rust crate writes here) so we
``CREATE TABLE IF NOT EXISTS`` on open and let the file appear on first
use, matching the convention in
``python/packages/corlinman-persona/src/corlinman_persona/store.py``.

All times are unix milliseconds.

Iter 1 ships only the schema + insert/list primitives the round-trip and
multi-tenant tests need. Window math, tier-derived target dates, and the
full CRUD surface arrive in iter 2; the placeholder resolver in iter 3.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from corlinman_goals.state import (
    SOURCE_VALUES,
    STATUS_VALUES,
    TIER_VALUES,
    Goal,
    GoalEvaluation,
)

# ``tenant_id`` defaults to ``'default'`` for single-tenant callers, and
# participates in the composite scope so the same ``agent_id`` can coexist
# across tenants without overwriting goals.
DEFAULT_TENANT_ID = "default"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS goals (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    agent_id        TEXT NOT NULL,
    tier            TEXT NOT NULL CHECK (tier IN ('short','mid','long')),
    body            TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    target_date     INTEGER NOT NULL,
    parent_goal_id  TEXT REFERENCES goals(id) ON DELETE SET NULL,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','completed','expired','archived')),
    source          TEXT NOT NULL
                    CHECK (source IN ('operator_cli','operator_ui','agent_self','seed'))
);

CREATE INDEX IF NOT EXISTS idx_goals_tenant_agent_tier_status
    ON goals(tenant_id, agent_id, tier, status);

CREATE INDEX IF NOT EXISTS idx_goals_parent
    ON goals(parent_goal_id) WHERE parent_goal_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS goal_evaluations (
    goal_id              TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    evaluated_at         INTEGER NOT NULL,
    score_0_to_10        INTEGER NOT NULL CHECK (score_0_to_10 BETWEEN 0 AND 10),
    narrative            TEXT NOT NULL,
    evidence_episode_ids TEXT NOT NULL,
    reflection_run_id    TEXT NOT NULL,
    PRIMARY KEY (goal_id, evaluated_at)
);

CREATE INDEX IF NOT EXISTS idx_goal_eval_recent
    ON goal_evaluations(goal_id, evaluated_at DESC);
"""


async def _table_exists(conn: aiosqlite.Connection, table: str) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row is not None


async def _column_exists(
    conn: aiosqlite.Connection, table: str, column: str
) -> bool:
    """True iff ``table.column`` exists. Used by future migration ALTERs.

    Mirrors ``corlinman_persona.store._column_exists`` so the two stores
    keep the same migration posture as the schema evolves.
    """
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    await cursor.close()
    return any(str(r[1]) == column for r in rows)


async def _ensure_schema(conn: aiosqlite.Connection) -> None:
    """Create both tables + indexes if missing.

    Iter 1 has no in-place migrations; the schema is born at this version.
    Future ALTERs go here, gated on :func:`_column_exists` to stay
    idempotent across reopens.
    """
    await conn.executescript(SCHEMA_SQL)
    # Foreign keys are off by default in SQLite — turn them on so
    # ``ON DELETE CASCADE`` actually fires on goal deletion (test:
    # ``goal_eval_cascades_on_goal_delete``).
    await conn.execute("PRAGMA foreign_keys = ON")


def _encode_evidence(ids: list[str]) -> str:
    """Persist evidence ids as a JSON array.

    Empty list serialises to ``"[]"`` (not ``""``) so the column stays
    JSON-decodable for callers using sqlite3 directly.
    """
    return json.dumps([str(i) for i in ids])


def _decode_evidence(raw: str) -> list[str]:
    """Inverse of :func:`_encode_evidence`. Corrupt rows degrade to ``[]``
    so the resolver can't crash on a tampered cell."""
    try:
        parsed = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(i) for i in parsed]


def _row_to_goal(row: aiosqlite.Row | tuple[Any, ...]) -> Goal:
    return Goal(
        id=str(row[0]),
        agent_id=str(row[1]),
        tier=str(row[2]),
        body=str(row[3]),
        created_at_ms=int(row[4]),
        target_date_ms=int(row[5]),
        parent_goal_id=None if row[6] is None else str(row[6]),
        status=str(row[7]),
        source=str(row[8]),
    )


def _row_to_eval(row: aiosqlite.Row | tuple[Any, ...]) -> GoalEvaluation:
    return GoalEvaluation(
        goal_id=str(row[0]),
        evaluated_at_ms=int(row[1]),
        score_0_to_10=int(row[2]),
        narrative=str(row[3]),
        evidence_episode_ids=_decode_evidence(str(row[4])),
        reflection_run_id=str(row[5]),
    )


def _validate_goal(goal: Goal) -> None:
    """Raise ``ValueError`` for tier/status/source values the schema would
    reject. The CHECK constraints will catch these too, but a Python-side
    error message is friendlier for CLI callers (iter 4)."""
    if goal.tier not in TIER_VALUES:
        raise ValueError(f"goal.tier={goal.tier!r} not in {sorted(TIER_VALUES)}")
    if goal.status not in STATUS_VALUES:
        raise ValueError(
            f"goal.status={goal.status!r} not in {sorted(STATUS_VALUES)}"
        )
    if goal.source not in SOURCE_VALUES:
        raise ValueError(
            f"goal.source={goal.source!r} not in {sorted(SOURCE_VALUES)}"
        )


class GoalStore:
    """Async wrapper around ``agent_goals.sqlite``.

    Use as an async context manager so the connection closes cleanly even
    if the caller aborts mid-task. Mirrors :class:`PersonaStore` so the
    operator can build a mental model once and reuse it across stores.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @classmethod
    async def open_or_create(cls, path: Path) -> GoalStore:
        """Open the DB (creating the file + schema if absent) and return
        an entered store.

        Convenience for callers that don't need ``async with`` framing
        (CLI subcommands, single-shot tests). Caller is responsible for
        ``await store.close()``.
        """
        store = cls(path)
        await store._open()
        return store

    async def __aenter__(self) -> GoalStore:
        await self._open()
        return self

    async def __aexit__(
        self, exc_type: object, exc: object, tb: object
    ) -> None:
        await self.close()

    async def _open(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        await _ensure_schema(self._conn)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("GoalStore used outside async context")
        return self._conn

    # ------------------------------------------------------------------
    # Goals — insert / list (iter 1)
    #
    # The full CRUD surface (update, archive, cascade) lands in iter 2 once
    # tier-derived ``target_date`` math is in place. Iter 1 only needs the
    # primitives the round-trip and tenant-isolation tests touch.
    # ------------------------------------------------------------------

    async def insert_goal(
        self,
        goal: Goal,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> None:
        """Insert one row into ``goals``. Raises on duplicate ``id``.

        Validates tier/status/source against the dataclass-level allow-set
        before hitting the DB so callers get a Python ``ValueError`` (with
        the field name) instead of a generic SQLite CHECK failure.
        """
        _validate_goal(goal)
        await self.conn.execute(
            """INSERT INTO goals
                 (id, tenant_id, agent_id, tier, body, created_at,
                  target_date, parent_goal_id, status, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                goal.id,
                tenant_id,
                goal.agent_id,
                goal.tier,
                goal.body,
                goal.created_at_ms,
                goal.target_date_ms,
                goal.parent_goal_id,
                goal.status,
                goal.source,
            ),
        )
        await self.conn.commit()

    async def get_goal(
        self,
        goal_id: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> Goal | None:
        """Return the row with id ``goal_id`` scoped to ``tenant_id`` or
        ``None``. Tenant filter is mandatory — operators must not see
        cross-tenant goals even by id."""
        cursor = await self.conn.execute(
            """SELECT id, agent_id, tier, body, created_at, target_date,
                      parent_goal_id, status, source
               FROM goals
               WHERE id = ? AND tenant_id = ?""",
            (goal_id, tenant_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else _row_to_goal(row)

    async def list_goals(
        self,
        *,
        agent_id: str | None = None,
        tier: str | None = None,
        status: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> list[Goal]:
        """Filtered list, ordered by ``created_at`` ascending.

        ``agent_id`` / ``tier`` / ``status`` are optional but composable —
        each maps to one ``AND <col> = ?`` clause. Sort order is the
        same one ``{{goals.today}}`` will reuse in iter 3, so the
        placeholder doesn't have to re-sort.
        """
        clauses = ["tenant_id = ?"]
        params: list[object] = [tenant_id]
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if tier is not None:
            if tier not in TIER_VALUES:
                raise ValueError(f"tier={tier!r} not in {sorted(TIER_VALUES)}")
            clauses.append("tier = ?")
            params.append(tier)
        if status is not None:
            if status not in STATUS_VALUES:
                raise ValueError(
                    f"status={status!r} not in {sorted(STATUS_VALUES)}"
                )
            clauses.append("status = ?")
            params.append(status)
        where = " AND ".join(clauses)
        cursor = await self.conn.execute(
            f"""SELECT id, agent_id, tier, body, created_at, target_date,
                       parent_goal_id, status, source
                FROM goals
                WHERE {where}
                ORDER BY created_at ASC, id ASC""",
            params,
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_goal(r) for r in rows]

    # ------------------------------------------------------------------
    # Goal evaluations — insert (idempotent) / list
    #
    # The reflection job (iter 5+) is the only writer. Iter 1 ships the
    # primitives so the round-trip + cascade tests can exercise the
    # ``ON DELETE CASCADE`` path before iter 5 wires the LLM.
    # ------------------------------------------------------------------

    async def insert_evaluation(
        self,
        evaluation: GoalEvaluation,
    ) -> bool:
        """Idempotently insert one ``goal_evaluations`` row.

        Returns True iff a new row was written. Re-running the same
        ``(goal_id, evaluated_at_ms)`` is a no-op — the design's
        idempotency contract: ``INSERT OR IGNORE`` against the composite PK
        means a crash-and-resume can't double-count.
        """
        if not 0 <= evaluation.score_0_to_10 <= 10:
            raise ValueError(
                f"score_0_to_10={evaluation.score_0_to_10} not in [0, 10]"
            )
        cursor = await self.conn.execute(
            """INSERT OR IGNORE INTO goal_evaluations
                 (goal_id, evaluated_at, score_0_to_10, narrative,
                  evidence_episode_ids, reflection_run_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                evaluation.goal_id,
                evaluation.evaluated_at_ms,
                int(evaluation.score_0_to_10),
                evaluation.narrative,
                _encode_evidence(evaluation.evidence_episode_ids),
                evaluation.reflection_run_id,
            ),
        )
        await self.conn.commit()
        wrote = cursor.rowcount > 0
        await cursor.close()
        return wrote

    # ------------------------------------------------------------------
    # Goal mutations — update / archive (iter 2).
    #
    # The CLI in iter 4 layers parent-of-equal-or-lower-tier rejection
    # over these primitives. ``archive_goal`` with ``cascade=True`` walks
    # exactly one level — the design's
    # ``cascade_archive_walks_one_level`` test pins "direct children, not
    # grandchildren". Operators wanting deeper sweeps re-archive the
    # children manually.
    # ------------------------------------------------------------------

    async def update_goal(
        self,
        goal_id: str,
        *,
        body: str | None = None,
        target_date_ms: int | None = None,
        parent_goal_id: str | None = ...,  # type: ignore[assignment]
        status: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> bool:
        """Update one or more mutable columns. Returns True iff a row
        changed.

        ``parent_goal_id`` uses an Ellipsis sentinel because ``None`` is a
        valid target value (orphaning a goal). Passing the literal
        ``None`` clears the parent; omitting the kwarg leaves it
        untouched.

        Validates the new ``status`` against the dataclass-level allow-set
        before issuing the UPDATE; the CHECK constraint would reject it
        too but the Python error names the field.
        """
        sets: list[str] = []
        params: list[object] = []
        if body is not None:
            sets.append("body = ?")
            params.append(body)
        if target_date_ms is not None:
            sets.append("target_date = ?")
            params.append(int(target_date_ms))
        if parent_goal_id is not ...:
            sets.append("parent_goal_id = ?")
            params.append(parent_goal_id)
        if status is not None:
            if status not in STATUS_VALUES:
                raise ValueError(
                    f"status={status!r} not in {sorted(STATUS_VALUES)}"
                )
            sets.append("status = ?")
            params.append(status)
        if not sets:
            return False
        params.extend([goal_id, tenant_id])
        cursor = await self.conn.execute(
            f"""UPDATE goals SET {', '.join(sets)}
                WHERE id = ? AND tenant_id = ?""",
            params,
        )
        await self.conn.commit()
        changed = cursor.rowcount > 0
        await cursor.close()
        return changed

    async def archive_goal(
        self,
        goal_id: str,
        *,
        cascade: bool = False,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> int:
        """Set ``status='archived'`` on the goal (and, if ``cascade``,
        on its direct children).

        Returns the count of rows touched. ``cascade`` is single-level by
        design — the operator escalates manually if grandchildren need to
        go too. The descent is bounded by the schema's
        ``parent_goal_id`` column, so we never recurse into a cycle (the
        CLI rejects parent self-reference at write time).
        """
        archived = 0
        # Parent first so callers can re-query and see the parent already
        # archived even if the cascade fails later.
        cursor = await self.conn.execute(
            """UPDATE goals SET status = 'archived'
               WHERE id = ? AND tenant_id = ?""",
            (goal_id, tenant_id),
        )
        archived += cursor.rowcount
        await cursor.close()
        if cascade:
            cursor = await self.conn.execute(
                """UPDATE goals SET status = 'archived'
                   WHERE parent_goal_id = ? AND tenant_id = ?""",
                (goal_id, tenant_id),
            )
            archived += cursor.rowcount
            await cursor.close()
        await self.conn.commit()
        return archived

    async def list_evaluations(
        self,
        goal_id: str,
        *,
        limit: int | None = None,
    ) -> list[GoalEvaluation]:
        """Most-recent-first list of evaluations for ``goal_id``.

        ``limit`` caps the row count for placeholder lookups (the resolver
        only needs the latest 1-12 entries depending on tier, so we never
        page back further than that).
        """
        sql = (
            """SELECT goal_id, evaluated_at, score_0_to_10, narrative,
                       evidence_episode_ids, reflection_run_id
                FROM goal_evaluations
                WHERE goal_id = ?
                ORDER BY evaluated_at DESC"""
        )
        params: list[object] = [goal_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        cursor = await self.conn.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_eval(r) for r in rows]


__all__ = [
    "DEFAULT_TENANT_ID",
    "SCHEMA_SQL",
    "GoalStore",
]
