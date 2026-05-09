"""Evolution-signal emission on weekly goal underperformance.

Per design §"Why this exists" 2nd bullet — "Evolution loop needs failure
to learn":

    A weekly score < 5 emits an ``evolution_signals`` row with
    ``event_kind = "goal.weekly_failed"``; the engine clusters those
    into ``skill_update`` candidates exactly the way it clusters
    ``tool_failure`` today.

This module owns one operation: append one signal row to the per-tenant
``evolution.sqlite`` D2 already mounts at the same data dir as
``agent_goals.sqlite``. The schema itself is owned by Rust
(:mod:`corlinman-evolution::schema` — `evolution_signals` was added in
W1 4-1A with the optional ``tenant_id`` column we propagate).

The function is a **best-effort hook**: a missing
``evolution.sqlite`` (the engine isn't deployed yet) logs a warning
and returns ``False`` rather than failing the reflection run. The
reflection job's primary contract is to write ``goal_evaluations``;
the evolution signal is downstream noise the goals package can't be
allowed to break.

Idempotency: ``evolution_signals.id`` is autoincrement so re-running a
reflection that already emitted a signal will write a second row. The
engine's clustering layer dedups by ``(event_kind, target)`` window
(:mod:`corlinman_evolution_engine.clustering`); we don't dedup at
emit-time. If a future reflection iteration wants a stronger
guarantee, the right place is a `(target, observed_at)` UNIQUE index
on the engine side — not here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Final

import aiosqlite

from corlinman_goals.state import Goal, GoalEvaluation

logger = logging.getLogger(__name__)

# Threshold: score < 5 fires the signal. The design pins this number;
# operators tune higher tolerances by adjusting reflection prompt
# rubrics, not by changing the threshold.
FAILURE_THRESHOLD: Final[int] = 5

# Event kind written into ``evolution_signals.event_kind``. Matches the
# design verbatim so the engine's clustering layer can recognise it
# alongside ``tool_failure`` / ``prompt_template_drift`` etc.
GOAL_WEEKLY_FAILED_EVENT_KIND: Final[str] = "goal.weekly_failed"

# Severity. Maps to the engine's existing severity vocabulary
# (``info`` / ``warn`` / ``critical``); ``warn`` puts the signal in
# the same bucket as a ``tool_failure`` cluster — surface-able but not
# pager-worthy.
DEFAULT_SEVERITY: Final[str] = "warn"


async def _column_present(
    conn: aiosqlite.Connection, table: str, column: str
) -> bool:
    """``True`` when ``table.column`` exists in the live schema.

    Same probe shape as
    :func:`corlinman_evolution_engine.store._column_present` — keeps
    this module dialect-aware so the older pre-W1-4-1A schemas (no
    ``tenant_id`` column) still accept signals.
    """
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    await cursor.close()
    return any(str(r[1]) == column for r in rows)


async def emit_goal_weekly_failed(
    *,
    evolution_db: Path,
    goal: Goal,
    evaluation: GoalEvaluation,
    tenant_id: str = "default",
    observed_at_ms: int | None = None,
) -> bool:
    """Insert one ``goal.weekly_failed`` row into ``evolution.sqlite``.

    Returns True iff a row was written. False on any of:

    * The DB file doesn't exist (engine not deployed).
    * The ``evolution_signals`` table is missing (Rust schema older
      than 4-1A's signal additions).
    * The score is at or above :data:`FAILURE_THRESHOLD` (caller
      filters but we double-check).
    * The goal isn't mid-tier (long/short underperformance is not the
      evolution-loop trigger surface — design pins "weekly score < 5"
      to mid).
    * An IO/SQL error fires (logged, not raised — emission is
      best-effort).

    ``observed_at_ms`` defaults to the evaluation's ``evaluated_at_ms``
    so the signal lands in the same wall-clock bucket as the
    underlying evaluation, which matters for the engine's run-window
    cluster query.
    """
    if evaluation.score_0_to_10 >= FAILURE_THRESHOLD:
        return False
    if goal.tier != "mid":
        return False
    if not evolution_db.exists():
        logger.info(
            "goal.weekly_failed signal skipped: evolution_db not present "
            "at %s",
            evolution_db,
        )
        return False

    observed = (
        observed_at_ms
        if observed_at_ms is not None
        else evaluation.evaluated_at_ms
    )

    payload = {
        "goal_id": goal.id,
        "agent_id": goal.agent_id,
        "tier": goal.tier,
        "body": goal.body,
        "score_0_to_10": evaluation.score_0_to_10,
        "narrative": evaluation.narrative,
        "evidence_episode_ids": list(evaluation.evidence_episode_ids),
        "reflection_run_id": evaluation.reflection_run_id,
    }

    try:
        conn = await aiosqlite.connect(evolution_db)
    except aiosqlite.Error as exc:
        logger.warning(
            "goal.weekly_failed signal: cannot open %s: %s",
            evolution_db,
            exc,
        )
        return False

    try:
        # Probe whether the table is even there. A reflection running
        # ahead of the Rust applier's schema bootstrap (e.g. fresh
        # tenant on a partially-rolled-out deploy) just gets a noop.
        cursor = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'evolution_signals'"
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            logger.info(
                "goal.weekly_failed signal skipped: evolution_signals "
                "table missing in %s",
                evolution_db,
            )
            return False

        has_tenant = await _column_present(
            conn, "evolution_signals", "tenant_id"
        )
        if has_tenant:
            await conn.execute(
                """INSERT INTO evolution_signals
                     (event_kind, target, severity, payload_json,
                      trace_id, session_id, observed_at, tenant_id)
                   VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)""",
                (
                    GOAL_WEEKLY_FAILED_EVENT_KIND,
                    f"goal:{goal.id}",
                    DEFAULT_SEVERITY,
                    json.dumps(payload),
                    int(observed),
                    tenant_id,
                ),
            )
        else:
            await conn.execute(
                """INSERT INTO evolution_signals
                     (event_kind, target, severity, payload_json,
                      trace_id, session_id, observed_at)
                   VALUES (?, ?, ?, ?, NULL, NULL, ?)""",
                (
                    GOAL_WEEKLY_FAILED_EVENT_KIND,
                    f"goal:{goal.id}",
                    DEFAULT_SEVERITY,
                    json.dumps(payload),
                    int(observed),
                ),
            )
        await conn.commit()
        logger.info(
            "emitted goal.weekly_failed signal goal_id=%s score=%s "
            "tenant=%s",
            goal.id,
            evaluation.score_0_to_10,
            tenant_id,
        )
        return True
    except aiosqlite.Error as exc:
        logger.warning(
            "goal.weekly_failed signal: SQL error inserting into %s: %s",
            evolution_db,
            exc,
        )
        return False
    finally:
        await conn.close()


__all__ = [
    "DEFAULT_SEVERITY",
    "FAILURE_THRESHOLD",
    "GOAL_WEEKLY_FAILED_EVENT_KIND",
    "emit_goal_weekly_failed",
]
