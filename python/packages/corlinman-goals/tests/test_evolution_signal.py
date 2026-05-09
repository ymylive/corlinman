"""Tests for :mod:`corlinman_goals.evolution_signal` (iter 9).

Pin design row ``goal_failure_emits_evolution_signal``:

    weekly score < 5 → row written to ``evolution_signals`` with
    ``event_kind = "goal.weekly_failed"``.

Plus the best-effort guards: missing DB is a noop, missing table is a
noop, score >= 5 is a noop, non-mid tier is a noop.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest
from corlinman_goals.evidence import StaticEvidence
from corlinman_goals.evolution_signal import (
    FAILURE_THRESHOLD,
    GOAL_WEEKLY_FAILED_EVENT_KIND,
    emit_goal_weekly_failed,
)
from corlinman_goals.reflection import (
    make_constant_grader,
    reflect_once,
)
from corlinman_goals.state import Goal, GoalEvaluation
from corlinman_goals.store import GoalStore

_NOW = int(datetime(2026, 5, 9, 14, 0, tzinfo=UTC).timestamp() * 1000)
_DAY_MS = 86_400 * 1000


# Authoritative schema lifted from the Rust crate
# ``corlinman-evolution::schema::SCHEMA_SQL`` — keeps the test honest
# about the live shape; if the Rust schema gains a column the
# emit-side will fail loudly here too.
EVOLUTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS evolution_signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_kind   TEXT NOT NULL,
    target       TEXT,
    severity     TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    trace_id     TEXT,
    session_id   TEXT,
    observed_at  INTEGER NOT NULL,
    tenant_id    TEXT NOT NULL DEFAULT 'default'
);
"""


async def _seed_evolution_db(path: Path, *, with_tenant: bool = True) -> None:
    """Materialise an empty ``evolution.sqlite`` matching the live schema."""
    schema = (
        EVOLUTION_SCHEMA
        if with_tenant
        else EVOLUTION_SCHEMA.replace(
            "tenant_id    TEXT NOT NULL DEFAULT 'default'", ""
        ).replace(",\n    \n", "\n")
    )
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(schema)
        await conn.commit()


def _mid_goal() -> Goal:
    return Goal(
        id="goal-2026-05-09-deepen-infra",
        agent_id="mentor",
        tier="mid",
        body="Become competent at infrastructure topics",
        created_at_ms=_NOW - 14 * _DAY_MS,
        target_date_ms=_NOW + 7 * _DAY_MS,
        status="active",
        source="operator_cli",
    )


def _eval(*, score: int, narrative: str = "low signal") -> GoalEvaluation:
    return GoalEvaluation(
        goal_id="goal-2026-05-09-deepen-infra",
        evaluated_at_ms=_NOW,
        score_0_to_10=score,
        narrative=narrative,
        evidence_episode_ids=["ep-1", "ep-2"],
        reflection_run_id="mid-1234",
    )


@pytest.mark.asyncio
async def test_emit_writes_row_for_score_under_threshold(tmp_path: Path) -> None:
    db = tmp_path / "evolution.sqlite"
    await _seed_evolution_db(db)

    wrote = await emit_goal_weekly_failed(
        evolution_db=db,
        goal=_mid_goal(),
        evaluation=_eval(score=3),
        tenant_id="default",
    )
    assert wrote is True

    async with aiosqlite.connect(db) as conn:
        cur = await conn.execute(
            "SELECT event_kind, target, severity, payload_json, "
            "observed_at, tenant_id FROM evolution_signals"
        )
        rows = await cur.fetchall()
        await cur.close()

    assert len(rows) == 1
    event_kind, target, severity, payload_json, observed_at, tenant_id = rows[0]
    assert event_kind == GOAL_WEEKLY_FAILED_EVENT_KIND
    assert target == "goal:goal-2026-05-09-deepen-infra"
    assert severity == "warn"
    assert observed_at == _NOW
    assert tenant_id == "default"
    payload = json.loads(payload_json)
    assert payload["goal_id"] == "goal-2026-05-09-deepen-infra"
    assert payload["score_0_to_10"] == 3
    assert payload["evidence_episode_ids"] == ["ep-1", "ep-2"]


@pytest.mark.asyncio
async def test_emit_noop_for_score_at_or_above_threshold(tmp_path: Path) -> None:
    db = tmp_path / "evolution.sqlite"
    await _seed_evolution_db(db)

    # Boundary: threshold itself does NOT fire.
    wrote = await emit_goal_weekly_failed(
        evolution_db=db,
        goal=_mid_goal(),
        evaluation=_eval(score=FAILURE_THRESHOLD),
    )
    assert wrote is False

    async with aiosqlite.connect(db) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM evolution_signals")
        row = await cur.fetchone()
        await cur.close()
    assert row is not None and row[0] == 0


@pytest.mark.asyncio
async def test_emit_noop_for_non_mid_tier(tmp_path: Path) -> None:
    db = tmp_path / "evolution.sqlite"
    await _seed_evolution_db(db)

    short_goal = Goal(
        id="goal-short-x",
        agent_id="mentor",
        tier="short",
        body="x",
        created_at_ms=_NOW - _DAY_MS,
        target_date_ms=_NOW + _DAY_MS,
        status="active",
        source="operator_cli",
    )
    wrote = await emit_goal_weekly_failed(
        evolution_db=db,
        goal=short_goal,
        evaluation=_eval(score=2),
    )
    assert wrote is False


@pytest.mark.asyncio
async def test_emit_noop_when_db_missing(tmp_path: Path) -> None:
    """No evolution.sqlite at all → silent noop, no exception."""
    missing = tmp_path / "does-not-exist.sqlite"
    wrote = await emit_goal_weekly_failed(
        evolution_db=missing,
        goal=_mid_goal(),
        evaluation=_eval(score=2),
    )
    assert wrote is False


@pytest.mark.asyncio
async def test_emit_noop_when_table_missing(tmp_path: Path) -> None:
    """evolution.sqlite exists but has no evolution_signals table."""
    db = tmp_path / "evolution.sqlite"
    async with aiosqlite.connect(db) as conn:
        await conn.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
        await conn.commit()

    wrote = await emit_goal_weekly_failed(
        evolution_db=db,
        goal=_mid_goal(),
        evaluation=_eval(score=2),
    )
    assert wrote is False


# ---------------------------------------------------------------------------
# Integration through ``reflect_once``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflect_once_emits_signal_on_mid_failure(tmp_path: Path) -> None:
    """End-to-end via :func:`reflect_once` — design's
    ``goal_failure_emits_evolution_signal`` row.
    """
    goals_db = tmp_path / "agent_goals.sqlite"
    evolution_db = tmp_path / "evolution.sqlite"
    await _seed_evolution_db(evolution_db)

    async with GoalStore(goals_db) as store:
        await store.insert_goal(_mid_goal())

        async def _grade(*, goal, window, evidence):
            from corlinman_goals.reflection import GraderReply
            return GraderReply(
                score_0_to_10=3,
                narrative="agent only mentioned infra twice this week",
                cited_episode_ids=[],
            )

        # Inject one episode so the LLM path is taken (otherwise the
        # runner short-circuits to the no-evidence sentinel which
        # never emits a signal).
        from corlinman_goals.evidence import EvidenceEpisode

        evidence_with_one = StaticEvidence(
            [
                EvidenceEpisode(
                    episode_id="ep-1",
                    started_at_ms=_NOW - 3 * _DAY_MS,
                    ended_at_ms=_NOW - 3 * _DAY_MS + 60_000,
                    kind="conversation",
                    summary_text="user asked about tcp tuning",
                    importance_score=0.4,
                )
            ]
        )

        summary = await reflect_once(
            store=store,
            evidence_source=evidence_with_one,
            grader=_grade,
            tier="mid",
            agent_id="mentor",
            now_ms=_NOW,
            evolution_db=evolution_db,
        )

    assert summary.goals_scored == 1
    assert summary.signals_emitted == 1
    assert summary.signal_goal_ids == ["goal-2026-05-09-deepen-infra"]

    async with aiosqlite.connect(evolution_db) as conn:
        cur = await conn.execute(
            "SELECT event_kind, target FROM evolution_signals"
        )
        rows = await cur.fetchall()
        await cur.close()
    assert len(rows) == 1
    assert rows[0] == (
        GOAL_WEEKLY_FAILED_EVENT_KIND,
        "goal:goal-2026-05-09-deepen-infra",
    )


@pytest.mark.asyncio
async def test_reflect_once_no_signal_on_short_tier(tmp_path: Path) -> None:
    """Short-tier failures don't emit (signal is mid-only)."""
    goals_db = tmp_path / "agent_goals.sqlite"
    evolution_db = tmp_path / "evolution.sqlite"
    await _seed_evolution_db(evolution_db)

    async with GoalStore(goals_db) as store:
        await store.insert_goal(
            Goal(
                id="g-short",
                agent_id="mentor",
                tier="short",
                body="today",
                created_at_ms=_NOW - 6 * 3600 * 1000,
                target_date_ms=_NOW + _DAY_MS,
                status="active",
                source="operator_cli",
            )
        )

        from corlinman_goals.evidence import EvidenceEpisode

        ev = StaticEvidence(
            [
                EvidenceEpisode(
                    episode_id="ep-1",
                    started_at_ms=_NOW - 3 * 3600 * 1000,
                    ended_at_ms=_NOW - 3 * 3600 * 1000 + 60_000,
                    kind="conversation",
                    summary_text="x",
                    importance_score=0.4,
                )
            ]
        )

        summary = await reflect_once(
            store=store,
            evidence_source=ev,
            grader=make_constant_grader(score=1, narrative="bad"),
            tier="short",
            agent_id="mentor",
            now_ms=_NOW,
            evolution_db=evolution_db,
        )

    assert summary.goals_scored == 1
    assert summary.signals_emitted == 0

    async with aiosqlite.connect(evolution_db) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM evolution_signals")
        row = await cur.fetchone()
        await cur.close()
    assert row is not None and row[0] == 0
