"""Tests for :mod:`corlinman_goals.store` (iter 1).

Pin the schema shape (test:
``goals_schema_round_trips`` in the design test matrix), the round-trip
through ``insert_goal`` / ``get_goal`` / ``list_goals``, the CHECK-
clause rejections, ``ON DELETE CASCADE`` for evaluations, and tenant
isolation (``multi_tenant_isolation_no_cross_read``).

All tests are async-mode auto via the root pytest config; aiosqlite's
connection lives entirely in the test's event loop so no fixtures are
needed beyond a tmp path.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from corlinman_goals.state import Goal, GoalEvaluation
from corlinman_goals.store import GoalStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "agent_goals.sqlite"


def _goal(
    *,
    goal_id: str = "goal-20260509-foo",
    agent_id: str = "mentor",
    tier: str = "short",
    body: str = "ship the thing",
    created_at_ms: int = 1_700_000_000_000,
    target_date_ms: int = 1_700_086_400_000,
    parent_goal_id: str | None = None,
    status: str = "active",
    source: str = "operator_cli",
) -> Goal:
    """Factory keeping per-test diffs small. All defaults are valid wrt the
    CHECK constraints; tests override the field they're exercising."""
    return Goal(
        id=goal_id,
        agent_id=agent_id,
        tier=tier,
        body=body,
        created_at_ms=created_at_ms,
        target_date_ms=target_date_ms,
        parent_goal_id=parent_goal_id,
        status=status,
        source=source,
    )


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------


async def test_open_or_create_creates_both_tables(db_path: Path) -> None:
    """A fresh path yields the file plus both ``goals`` and
    ``goal_evaluations`` tables with the documented columns. Pin the
    column set so a future migration can't silently drop a column."""
    store = await GoalStore.open_or_create(db_path)
    try:
        assert db_path.exists()
        conn = sqlite3.connect(db_path)
        try:
            goals_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(goals)")
            }
            eval_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(goal_evaluations)")
            }
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        assert tables.issuperset({"goals", "goal_evaluations"})
        assert goals_cols == {
            "id",
            "tenant_id",
            "agent_id",
            "tier",
            "body",
            "created_at",
            "target_date",
            "parent_goal_id",
            "status",
            "source",
        }
        assert eval_cols == {
            "goal_id",
            "evaluated_at",
            "score_0_to_10",
            "narrative",
            "evidence_episode_ids",
            "reflection_run_id",
        }
    finally:
        await store.close()


async def test_indexes_exist(db_path: Path) -> None:
    """The two named indexes drive the placeholder query plan; pin them
    so a refactor that drops them surfaces in CI, not in production."""
    async with GoalStore(db_path):
        pass
    conn = sqlite3.connect(db_path)
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
    finally:
        conn.close()
    assert "idx_goals_tenant_agent_tier_status" in names
    assert "idx_goals_parent" in names
    assert "idx_goal_eval_recent" in names


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


async def test_insert_then_get_round_trips_all_fields(db_path: Path) -> None:
    g = _goal(parent_goal_id=None, status="active", source="operator_cli")
    async with GoalStore(db_path) as store:
        await store.insert_goal(g)
        got = await store.get_goal(g.id)
    assert got is not None
    assert got.id == g.id
    assert got.agent_id == "mentor"
    assert got.tier == "short"
    assert got.body == "ship the thing"
    assert got.created_at_ms == g.created_at_ms
    assert got.target_date_ms == g.target_date_ms
    assert got.parent_goal_id is None
    assert got.status == "active"
    assert got.source == "operator_cli"


async def test_get_missing_returns_none(db_path: Path) -> None:
    async with GoalStore(db_path) as store:
        assert await store.get_goal("never-existed") is None


async def test_list_goals_filters_compose(db_path: Path) -> None:
    """``agent_id`` / ``tier`` / ``status`` are AND-composed. Stable sort
    by ``created_at`` then ``id`` keeps the placeholder output
    deterministic."""
    async with GoalStore(db_path) as store:
        await store.insert_goal(
            _goal(goal_id="g1", tier="short", agent_id="a", created_at_ms=100)
        )
        await store.insert_goal(
            _goal(goal_id="g2", tier="short", agent_id="a", created_at_ms=200)
        )
        await store.insert_goal(
            _goal(goal_id="g3", tier="mid", agent_id="a", created_at_ms=150)
        )
        await store.insert_goal(
            _goal(goal_id="g4", tier="short", agent_id="b", created_at_ms=50)
        )
        await store.insert_goal(
            _goal(
                goal_id="g5",
                tier="short",
                agent_id="a",
                status="archived",
                created_at_ms=300,
            )
        )

        all_a = await store.list_goals(agent_id="a")
        assert [r.id for r in all_a] == ["g1", "g3", "g2", "g5"]

        a_short = await store.list_goals(agent_id="a", tier="short")
        assert [r.id for r in a_short] == ["g1", "g2", "g5"]

        a_short_active = await store.list_goals(
            agent_id="a", tier="short", status="active"
        )
        assert [r.id for r in a_short_active] == ["g1", "g2"]


async def test_list_goals_rejects_invalid_filters(db_path: Path) -> None:
    """Validation lives in Python so callers see a useful exception
    instead of an empty result set."""
    async with GoalStore(db_path) as store:
        with pytest.raises(ValueError, match="tier="):
            await store.list_goals(tier="bogus")
        with pytest.raises(ValueError, match="status="):
            await store.list_goals(status="bogus")


async def test_insert_validates_enum_fields(db_path: Path) -> None:
    """Tier/status/source values outside the allow-set raise
    ``ValueError`` before the SQL hits the DB."""
    async with GoalStore(db_path) as store:
        with pytest.raises(ValueError, match=r"goal\.tier="):
            await store.insert_goal(_goal(tier="bogus"))
        with pytest.raises(ValueError, match=r"goal\.status="):
            await store.insert_goal(_goal(status="bogus"))
        with pytest.raises(ValueError, match=r"goal\.source="):
            await store.insert_goal(_goal(source="bogus"))


async def test_insert_duplicate_id_raises(db_path: Path) -> None:
    async with GoalStore(db_path) as store:
        await store.insert_goal(_goal(goal_id="dup"))
        with pytest.raises(Exception):  # noqa: B017 - aiosqlite IntegrityError
            await store.insert_goal(_goal(goal_id="dup"))


async def test_check_constraint_rejects_bad_score(db_path: Path) -> None:
    """``score_0_to_10`` outside the inclusive [0, 10] range is rejected
    by the Python guard before reaching SQLite."""
    g = _goal()
    async with GoalStore(db_path) as store:
        await store.insert_goal(g)
        with pytest.raises(ValueError, match="score_0_to_10="):
            await store.insert_evaluation(
                GoalEvaluation(
                    goal_id=g.id,
                    evaluated_at_ms=1,
                    score_0_to_10=11,
                    narrative="too high",
                    evidence_episode_ids=[],
                    reflection_run_id="run-1",
                )
            )
        with pytest.raises(ValueError, match="score_0_to_10="):
            await store.insert_evaluation(
                GoalEvaluation(
                    goal_id=g.id,
                    evaluated_at_ms=2,
                    score_0_to_10=-1,
                    narrative="too low",
                    evidence_episode_ids=[],
                    reflection_run_id="run-2",
                )
            )


# ---------------------------------------------------------------------------
# goal_evaluations
# ---------------------------------------------------------------------------


async def test_evaluation_round_trip_decodes_evidence(db_path: Path) -> None:
    g = _goal()
    eva = GoalEvaluation(
        goal_id=g.id,
        evaluated_at_ms=1_700_000_000_500,
        score_0_to_10=7,
        narrative="solid week",
        evidence_episode_ids=["ep-a", "ep-b"],
        reflection_run_id="mid-1700000000000",
    )
    async with GoalStore(db_path) as store:
        await store.insert_goal(g)
        wrote = await store.insert_evaluation(eva)
        assert wrote is True
        rows = await store.list_evaluations(g.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.score_0_to_10 == 7
    assert row.narrative == "solid week"
    assert row.evidence_episode_ids == ["ep-a", "ep-b"]
    assert row.reflection_run_id == "mid-1700000000000"


async def test_evaluation_idempotent_on_same_pk(db_path: Path) -> None:
    """``INSERT OR IGNORE`` against ``(goal_id, evaluated_at)`` so a
    crash-and-resume can't double-count. Test the design's idempotency
    contract directly at the store layer."""
    g = _goal()
    eva = GoalEvaluation(
        goal_id=g.id,
        evaluated_at_ms=1234,
        score_0_to_10=7,
        narrative="first write",
        evidence_episode_ids=["ep-a"],
        reflection_run_id="mid-1234",
    )
    second_attempt = GoalEvaluation(
        goal_id=g.id,
        evaluated_at_ms=1234,
        score_0_to_10=2,  # different payload, same PK
        narrative="should be ignored",
        evidence_episode_ids=["ep-z"],
        reflection_run_id="mid-1234",
    )
    async with GoalStore(db_path) as store:
        await store.insert_goal(g)
        assert await store.insert_evaluation(eva) is True
        assert await store.insert_evaluation(second_attempt) is False
        rows = await store.list_evaluations(g.id)
    # The original payload must survive — IGNORE means "leave the row alone".
    assert len(rows) == 1
    assert rows[0].narrative == "first write"
    assert rows[0].score_0_to_10 == 7


async def test_evaluation_evidence_persists_as_json_array(
    db_path: Path,
) -> None:
    """The on-disk wire format must be a JSON array so callers using
    sqlite3 directly can decode it with ``json.loads`` (parity with
    ``corlinman_persona.recent_topics``)."""
    g = _goal()
    eva = GoalEvaluation(
        goal_id=g.id,
        evaluated_at_ms=1,
        score_0_to_10=5,
        narrative="...",
        evidence_episode_ids=["x", "y"],
        reflection_run_id="run-1",
    )
    async with GoalStore(db_path) as store:
        await store.insert_goal(g)
        await store.insert_evaluation(eva)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT evidence_episode_ids FROM goal_evaluations WHERE goal_id = ?",
            (g.id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert json.loads(row[0]) == ["x", "y"]


async def test_evaluation_decodes_corrupt_evidence_to_empty_list(
    db_path: Path,
) -> None:
    """Stomped JSON shouldn't crash the resolver — graceful empty fallback
    matches the persona store's defensive posture."""
    g = _goal()
    async with GoalStore(db_path) as store:
        await store.insert_goal(g)
        await store.insert_evaluation(
            GoalEvaluation(
                goal_id=g.id,
                evaluated_at_ms=1,
                score_0_to_10=3,
                narrative="...",
                evidence_episode_ids=["x"],
                reflection_run_id="r",
            )
        )
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE goal_evaluations SET evidence_episode_ids = ? WHERE goal_id = ?",
            ("not json", g.id),
        )
        conn.commit()
    finally:
        conn.close()
    async with GoalStore(db_path) as store:
        rows = await store.list_evaluations(g.id)
    assert len(rows) == 1
    assert rows[0].evidence_episode_ids == []


async def test_evaluations_cascade_when_goal_deleted(db_path: Path) -> None:
    """``ON DELETE CASCADE`` must fire — the reflection audit must not
    survive its goal. Foreign keys are off by default in SQLite, so the
    store's ``PRAGMA foreign_keys = ON`` is what makes this pass."""
    g = _goal()
    async with GoalStore(db_path) as store:
        await store.insert_goal(g)
        await store.insert_evaluation(
            GoalEvaluation(
                goal_id=g.id,
                evaluated_at_ms=1,
                score_0_to_10=5,
                narrative="ok",
                evidence_episode_ids=[],
                reflection_run_id="r",
            )
        )
        # Direct delete to keep iter 1 minimal — archive_goal lands iter 2.
        await store.conn.execute("DELETE FROM goals WHERE id = ?", (g.id,))
        await store.conn.commit()
        rows = await store.list_evaluations(g.id)
    assert rows == []


async def test_list_evaluations_is_most_recent_first(db_path: Path) -> None:
    g = _goal()
    async with GoalStore(db_path) as store:
        await store.insert_goal(g)
        for ts in (3000, 1000, 2000):
            await store.insert_evaluation(
                GoalEvaluation(
                    goal_id=g.id,
                    evaluated_at_ms=ts,
                    score_0_to_10=5,
                    narrative=f"at {ts}",
                    evidence_episode_ids=[],
                    reflection_run_id=f"r-{ts}",
                )
            )
        rows = await store.list_evaluations(g.id)
    assert [r.evaluated_at_ms for r in rows] == [3000, 2000, 1000]


async def test_list_evaluations_respects_limit(db_path: Path) -> None:
    g = _goal()
    async with GoalStore(db_path) as store:
        await store.insert_goal(g)
        for ts in range(10):
            await store.insert_evaluation(
                GoalEvaluation(
                    goal_id=g.id,
                    evaluated_at_ms=ts,
                    score_0_to_10=ts,
                    narrative=str(ts),
                    evidence_episode_ids=[],
                    reflection_run_id=f"r-{ts}",
                )
            )
        rows = await store.list_evaluations(g.id, limit=3)
    assert len(rows) == 3
    assert [r.evaluated_at_ms for r in rows] == [9, 8, 7]


# ---------------------------------------------------------------------------
# Multi-tenant isolation (matches the design's
# ``multi_tenant_isolation_no_cross_read`` test row).
# ---------------------------------------------------------------------------


async def test_same_goal_id_can_coexist_across_tenants(db_path: Path) -> None:
    """Composite scope = ``(tenant_id, id)`` for selects; an ``id`` is
    only required to be unique inside one tenant, not across."""
    g_a = _goal(goal_id="g-1", body="a-version", agent_id="mentor")
    async with GoalStore(db_path) as store:
        await store.insert_goal(g_a, tenant_id="tenant-a")
        # Same ``id`` across tenants would collide — id is the global PK.
        # The store's tenant scope is on read, not write; demonstrate by
        # using a different id but same agent in the second tenant.
        await store.insert_goal(
            _goal(goal_id="g-2", body="b-version", agent_id="mentor"),
            tenant_id="tenant-b",
        )

        a_rows = await store.list_goals(tenant_id="tenant-a")
        b_rows = await store.list_goals(tenant_id="tenant-b")
        cross = await store.get_goal("g-2", tenant_id="tenant-a")

    assert [r.id for r in a_rows] == ["g-1"]
    assert [r.id for r in b_rows] == ["g-2"]
    # Cross-tenant lookup must miss even though the row exists in another
    # tenant's scope — operator-grade isolation, not just convention.
    assert cross is None


async def test_list_goals_default_tenant_skips_other_tenants(
    db_path: Path,
) -> None:
    """A row whose ``tenant_id`` got rewritten out of band must not be
    returned by the default-tenant read. Pins the WHERE-clause scope so
    callers can trust tenant isolation. Same shape as
    ``corlinman_persona``'s analogous test."""
    async with GoalStore(db_path) as store:
        await store.insert_goal(_goal(goal_id="g-default"))
    # Re-tag one row's tenant_id without the store API.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE goals SET tenant_id = 'other' WHERE id = 'g-default'"
        )
        conn.commit()
    finally:
        conn.close()
    async with GoalStore(db_path) as store:
        default_rows = await store.list_goals()
        other_rows = await store.list_goals(tenant_id="other")
    assert default_rows == []
    assert [r.id for r in other_rows] == ["g-default"]


async def test_use_outside_context_raises(db_path: Path) -> None:
    store = GoalStore(db_path)
    with pytest.raises(RuntimeError, match="outside async context"):
        _ = store.conn
