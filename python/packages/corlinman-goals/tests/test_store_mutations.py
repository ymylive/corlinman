"""Tests for the iter 2 mutation surface — ``update_goal`` and
``archive_goal``.

The CLI in iter 4 layers parent-of-equal-or-lower-tier rejection over
these primitives, so the store layer accepts any cross-tier shape and
only validates the field-level enums. Cascade is single-level by design
(``cascade_archive_walks_one_level``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_goals.state import Goal
from corlinman_goals.store import GoalStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "agent_goals.sqlite"


def _g(
    *,
    goal_id: str,
    tier: str = "short",
    status: str = "active",
    parent_goal_id: str | None = None,
    body: str = "do thing",
    created_at_ms: int = 100,
    target_date_ms: int = 1000,
    agent_id: str = "mentor",
) -> Goal:
    return Goal(
        id=goal_id,
        agent_id=agent_id,
        tier=tier,
        body=body,
        created_at_ms=created_at_ms,
        target_date_ms=target_date_ms,
        parent_goal_id=parent_goal_id,
        status=status,
        source="operator_cli",
    )


# ---------------------------------------------------------------------------
# update_goal
# ---------------------------------------------------------------------------


async def test_update_goal_changes_only_named_fields(db_path: Path) -> None:
    async with GoalStore(db_path) as store:
        await store.insert_goal(_g(goal_id="g1", body="old", target_date_ms=100))
        changed = await store.update_goal(
            "g1", body="new body", target_date_ms=999
        )
        got = await store.get_goal("g1")
    assert changed is True
    assert got is not None
    assert got.body == "new body"
    assert got.target_date_ms == 999
    # Untouched fields stay put.
    assert got.tier == "short"
    assert got.status == "active"


async def test_update_goal_status_validates_enum(db_path: Path) -> None:
    async with GoalStore(db_path) as store:
        await store.insert_goal(_g(goal_id="g1"))
        with pytest.raises(ValueError, match=r"status="):
            await store.update_goal("g1", status="bogus")


async def test_update_goal_can_set_status_to_completed(db_path: Path) -> None:
    """Completion is operator-only (per the design's "auto-completion
    rewards score gaming" rationale); the store accepts any allowed
    status string and the CLI gates the transition."""
    async with GoalStore(db_path) as store:
        await store.insert_goal(_g(goal_id="g1"))
        await store.update_goal("g1", status="completed")
        got = await store.get_goal("g1")
    assert got is not None
    assert got.status == "completed"


async def test_update_goal_can_clear_parent_with_explicit_none(
    db_path: Path,
) -> None:
    """``parent_goal_id=None`` means "orphan this goal"; omitting the
    kwarg means "leave it alone". The Ellipsis sentinel disambiguates."""
    async with GoalStore(db_path) as store:
        await store.insert_goal(_g(goal_id="parent", tier="mid"))
        await store.insert_goal(
            _g(goal_id="child", tier="short", parent_goal_id="parent")
        )
        # Sanity: parent is set.
        before = await store.get_goal("child")
        assert before is not None
        assert before.parent_goal_id == "parent"
        # Now clear it.
        await store.update_goal("child", parent_goal_id=None)
        after = await store.get_goal("child")
    assert after is not None
    assert after.parent_goal_id is None


async def test_update_goal_with_no_kwargs_is_noop(db_path: Path) -> None:
    """No-op pattern: the call is harmless if no kwargs are passed.
    Returns False so callers can branch on "did anything change?"."""
    async with GoalStore(db_path) as store:
        await store.insert_goal(_g(goal_id="g1", body="unchanged"))
        changed = await store.update_goal("g1")
        got = await store.get_goal("g1")
    assert changed is False
    assert got is not None
    assert got.body == "unchanged"


async def test_update_goal_missing_id_returns_false(db_path: Path) -> None:
    async with GoalStore(db_path) as store:
        changed = await store.update_goal("nope", body="...")
    assert changed is False


async def test_update_goal_respects_tenant_scope(db_path: Path) -> None:
    async with GoalStore(db_path) as store:
        await store.insert_goal(_g(goal_id="g1"), tenant_id="tenant-a")
        # Update aimed at the wrong tenant must not match.
        changed = await store.update_goal(
            "g1", body="new", tenant_id="tenant-b"
        )
        got_a = await store.get_goal("g1", tenant_id="tenant-a")
    assert changed is False
    assert got_a is not None
    assert got_a.body == "do thing"


# ---------------------------------------------------------------------------
# archive_goal — cascade is single-level (design pin)
# ---------------------------------------------------------------------------


async def test_archive_goal_sets_status_archived(db_path: Path) -> None:
    async with GoalStore(db_path) as store:
        await store.insert_goal(_g(goal_id="g1"))
        n = await store.archive_goal("g1")
        got = await store.get_goal("g1")
    assert n == 1
    assert got is not None
    assert got.status == "archived"


async def test_archive_goal_without_cascade_leaves_children(
    db_path: Path,
) -> None:
    async with GoalStore(db_path) as store:
        await store.insert_goal(_g(goal_id="parent", tier="mid"))
        await store.insert_goal(
            _g(goal_id="child", tier="short", parent_goal_id="parent")
        )
        n = await store.archive_goal("parent", cascade=False)
        parent = await store.get_goal("parent")
        child = await store.get_goal("child")
    assert n == 1
    assert parent is not None
    assert parent.status == "archived"
    assert child is not None
    assert child.status == "active"


async def test_archive_goal_cascade_walks_exactly_one_level(
    db_path: Path,
) -> None:
    """``cascade_archive_walks_one_level`` from the design test matrix:
    direct children archived, grandchildren left active. Operators
    wanting deeper sweeps re-archive manually."""
    async with GoalStore(db_path) as store:
        await store.insert_goal(_g(goal_id="long-1", tier="long"))
        await store.insert_goal(
            _g(goal_id="mid-1", tier="mid", parent_goal_id="long-1")
        )
        await store.insert_goal(
            _g(goal_id="short-1", tier="short", parent_goal_id="mid-1")
        )

        n = await store.archive_goal("long-1", cascade=True)
        long_g = await store.get_goal("long-1")
        mid_g = await store.get_goal("mid-1")
        short_g = await store.get_goal("short-1")

    assert n == 2  # parent + one direct child
    assert long_g is not None
    assert long_g.status == "archived"
    assert mid_g is not None
    assert mid_g.status == "archived"
    # Grandchild stays active — single-level descent only.
    assert short_g is not None
    assert short_g.status == "active"


async def test_archive_goal_cascade_does_not_cross_tenants(
    db_path: Path,
) -> None:
    """Distinct parent ids per tenant (the schema's ``id TEXT PRIMARY
    KEY`` is global). The cascade WHERE clause must scope by
    ``tenant_id``, not just by ``parent_goal_id``."""
    async with GoalStore(db_path) as store:
        await store.insert_goal(
            _g(goal_id="parent-a", tier="mid"), tenant_id="tenant-a"
        )
        await store.insert_goal(
            _g(goal_id="parent-b", tier="mid"), tenant_id="tenant-b"
        )
        await store.insert_goal(
            _g(goal_id="child-a", tier="short", parent_goal_id="parent-a"),
            tenant_id="tenant-a",
        )
        await store.insert_goal(
            _g(goal_id="child-b", tier="short", parent_goal_id="parent-b"),
            tenant_id="tenant-b",
        )

        n = await store.archive_goal(
            "parent-a", cascade=True, tenant_id="tenant-a"
        )
        a_child = await store.get_goal("child-a", tenant_id="tenant-a")
        b_parent = await store.get_goal("parent-b", tenant_id="tenant-b")
        b_child = await store.get_goal("child-b", tenant_id="tenant-b")

    # Parent-A and its direct child archived; tenant-B unaffected.
    assert n == 2
    assert a_child is not None
    assert a_child.status == "archived"
    assert b_parent is not None
    assert b_parent.status == "active"
    assert b_child is not None
    assert b_child.status == "active"


async def test_archive_goal_missing_id_returns_zero(db_path: Path) -> None:
    async with GoalStore(db_path) as store:
        n = await store.archive_goal("nope")
    assert n == 0
