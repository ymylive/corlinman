"""Phase 4 W4.2 — ``curator_state`` table + :class:`CuratorStateRepo`.

Mirrors the hermes ``agent/curator.py`` JSON bookkeeping shape but lives
in SQLite so the gateway can query / list across profiles.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from corlinman_evolution_store import (
    CuratorState,
    CuratorStateRepo,
    EvolutionStore,
)
from corlinman_evolution_store.store import _column_exists


# ---------------------------------------------------------------------------
# Schema presence
# ---------------------------------------------------------------------------


async def test_curator_state_table_exists_on_fresh_db(db_path: Path) -> None:
    """Fresh open materialises ``curator_state`` with every documented
    column."""
    async with await EvolutionStore.open(db_path) as store:
        cursor = await store.conn.execute("SELECT COUNT(*) FROM curator_state")
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None and int(row[0]) == 0, "curator_state starts empty"

        for col in (
            "profile_slug",
            "last_review_at",
            "last_review_duration_ms",
            "last_review_summary",
            "run_count",
            "paused",
            "interval_hours",
            "stale_after_days",
            "archive_after_days",
            "tenant_id",
        ):
            assert await _column_exists(store.conn, "curator_state", col), (
                f"curator_state.{col} should exist on fresh DB"
            )


async def test_curator_state_tenant_index_present(db_path: Path) -> None:
    """Tenant index ships with the schema so the admin list query
    doesn't full-scan once multi-tenant lands."""
    async with await EvolutionStore.open(db_path) as store:
        cursor = await store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            " AND name='idx_curator_state_tenant'",
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None, "idx_curator_state_tenant should exist"


async def test_legacy_db_without_curator_state_converges(db_path: Path) -> None:
    """A DB that pre-dates the W4.2 schema (no ``curator_state`` table)
    must converge on a normal :meth:`EvolutionStore.open` — the
    ``CREATE TABLE IF NOT EXISTS`` block is the only thing that adds
    the table, so this is the smoke test that the additive change is
    in the right place."""
    # Bootstrap a pre-W4.2 DB by hand: just one of the older tables, no
    # curator_state at all.
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.executescript(
            """CREATE TABLE evolution_signals (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_kind   TEXT NOT NULL,
                    target       TEXT,
                    severity     TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    trace_id     TEXT,
                    session_id   TEXT,
                    observed_at  INTEGER NOT NULL,
                    tenant_id    TEXT NOT NULL DEFAULT 'default'
                );"""
        )
        await conn.commit()
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='curator_state'"
        )
        assert await cursor.fetchone() is None
        await cursor.close()
    finally:
        await conn.close()

    async with await EvolutionStore.open(db_path) as store:
        cursor = await store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='curator_state'"
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None, "open() must add curator_state to a legacy DB"


# ---------------------------------------------------------------------------
# Repo behaviour — default-valued fetch, upsert, mark_run, list_all
# ---------------------------------------------------------------------------


async def test_get_returns_default_struct_when_missing(store: EvolutionStore) -> None:
    """``get`` for an unknown profile returns a synthetic struct rather
    than raising or returning ``None`` — DDL defaults baked in."""
    repo = CuratorStateRepo(store.conn)
    state = await repo.get("research")
    assert state.profile_slug == "research"
    assert state.last_review_at is None
    assert state.last_review_duration_ms is None
    assert state.last_review_summary is None
    assert state.run_count == 0
    assert state.paused is False
    assert state.interval_hours == 168
    assert state.stale_after_days == 30
    assert state.archive_after_days == 90
    assert state.tenant_id == "default"


async def test_upsert_then_get_roundtrip(store: EvolutionStore) -> None:
    """An explicit upsert is visible to a subsequent ``get``."""
    repo = CuratorStateRepo(store.conn)
    when = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    state = CuratorState(
        profile_slug="research",
        last_review_at=when,
        last_review_duration_ms=4_321,
        last_review_summary="marked 2 stale, archived 1",
        run_count=7,
        paused=True,
        interval_hours=72,
        stale_after_days=14,
        archive_after_days=60,
        tenant_id="default",
    )
    await repo.upsert(state)
    fetched = await repo.get("research")

    assert fetched.profile_slug == "research"
    assert fetched.last_review_at == when
    assert fetched.last_review_duration_ms == 4_321
    assert fetched.last_review_summary == "marked 2 stale, archived 1"
    assert fetched.run_count == 7
    assert fetched.paused is True
    assert fetched.interval_hours == 72
    assert fetched.stale_after_days == 14
    assert fetched.archive_after_days == 60
    assert fetched.tenant_id == "default"


async def test_upsert_is_idempotent_replace(store: EvolutionStore) -> None:
    """A second upsert overwrites — the table stays one row per slug."""
    repo = CuratorStateRepo(store.conn)
    base = CuratorState(
        profile_slug="research",
        last_review_at=datetime(2026, 5, 1, tzinfo=UTC),
        last_review_duration_ms=100,
        last_review_summary="first",
        run_count=1,
        paused=False,
        interval_hours=168,
        stale_after_days=30,
        archive_after_days=90,
        tenant_id="default",
    )
    await repo.upsert(base)
    later = CuratorState(
        profile_slug="research",
        last_review_at=datetime(2026, 5, 10, tzinfo=UTC),
        last_review_duration_ms=200,
        last_review_summary="second",
        run_count=2,
        paused=True,
        interval_hours=168,
        stale_after_days=30,
        archive_after_days=90,
        tenant_id="default",
    )
    await repo.upsert(later)

    cursor = await store.conn.execute(
        "SELECT COUNT(*) FROM curator_state WHERE profile_slug = ?",
        ("research",),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None and int(row[0]) == 1, "upsert must replace, not duplicate"

    fetched = await repo.get("research")
    assert fetched.run_count == 2
    assert fetched.paused is True
    assert fetched.last_review_summary == "second"


async def test_mark_run_bumps_count_and_stamps_review(store: EvolutionStore) -> None:
    """``mark_run`` against an unknown slug starts run_count at 1; a
    subsequent call advances to 2 and refreshes the timestamp / summary
    without touching the operator-tunable thresholds."""
    repo = CuratorStateRepo(store.conn)
    t1 = datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC)

    state1 = await repo.mark_run(
        "research", duration_ms=1_500, summary="first run", now=t1
    )
    assert state1.run_count == 1
    assert state1.last_review_at == t1
    assert state1.last_review_duration_ms == 1_500
    assert state1.last_review_summary == "first run"
    # Defaults preserved.
    assert state1.interval_hours == 168
    assert state1.stale_after_days == 30
    assert state1.archive_after_days == 90
    assert state1.paused is False

    # Tweak the thresholds — ``mark_run`` must not clobber them.
    await repo.upsert(
        CuratorState(
            profile_slug="research",
            last_review_at=t1,
            last_review_duration_ms=1_500,
            last_review_summary="first run",
            run_count=1,
            paused=False,
            interval_hours=72,
            stale_after_days=14,
            archive_after_days=60,
            tenant_id="default",
        )
    )

    t2 = datetime(2026, 5, 18, 9, 0, 0, tzinfo=UTC)
    state2 = await repo.mark_run(
        "research", duration_ms=2_000, summary="second run", now=t2
    )
    assert state2.run_count == 2
    assert state2.last_review_at == t2
    assert state2.last_review_summary == "second run"
    assert state2.interval_hours == 72, "operator-tuned interval preserved"
    assert state2.stale_after_days == 14
    assert state2.archive_after_days == 60


async def test_list_all_filters_by_tenant(store: EvolutionStore) -> None:
    """``list_all`` returns only rows for the requested tenant,
    sorted by ``profile_slug``.

    Note: ``profile_slug`` is the table's sole PRIMARY KEY (matches the
    DDL in W4.2 — a profile is globally unique), so tenants partition
    *which slugs* live where rather than allowing the same slug under
    two tenants."""
    repo = CuratorStateRepo(store.conn)
    when = datetime(2026, 5, 17, tzinfo=UTC)

    for slug, tenant in (
        ("research", "default"),
        ("notes", "default"),
        ("personal", "tenant-b"),
    ):
        await repo.upsert(
            CuratorState(
                profile_slug=slug,
                last_review_at=when,
                last_review_duration_ms=1,
                last_review_summary="x",
                run_count=1,
                paused=False,
                interval_hours=168,
                stale_after_days=30,
                archive_after_days=90,
                tenant_id=tenant,
            )
        )

    default_rows = await repo.list_all()
    assert [r.profile_slug for r in default_rows] == ["notes", "research"]
    assert all(r.tenant_id == "default" for r in default_rows)

    tenant_b_rows = await repo.list_all(tenant_id="tenant-b")
    assert [r.profile_slug for r in tenant_b_rows] == ["personal"]
    assert tenant_b_rows[0].tenant_id == "tenant-b"

    # Unknown tenant returns an empty list, not a synthetic row.
    other = await repo.list_all(tenant_id="tenant-z")
    assert other == []


async def test_get_filters_by_tenant_mismatch_returns_default(
    store: EvolutionStore,
) -> None:
    """Tenant scoping on ``get``: a slug that exists under ``default``
    but not under ``tenant-b`` resolves to the synthetic default row
    under ``tenant-b`` (does not leak across tenants)."""
    repo = CuratorStateRepo(store.conn)
    when = datetime(2026, 5, 17, tzinfo=UTC)

    await repo.upsert(
        CuratorState(
            profile_slug="research",
            last_review_at=when,
            last_review_duration_ms=10,
            last_review_summary="default tenant",
            run_count=3,
            paused=False,
            interval_hours=168,
            stale_after_days=30,
            archive_after_days=90,
            tenant_id="default",
        )
    )

    # Default tenant: real row.
    a = await repo.get("research")
    assert a.tenant_id == "default"
    assert a.run_count == 3
    assert a.paused is False

    # Other tenant: synthetic default, not the default-tenant row.
    b = await repo.get("research", tenant_id="tenant-b")
    assert b.tenant_id == "tenant-b"
    assert b.run_count == 0
    assert b.last_review_at is None
