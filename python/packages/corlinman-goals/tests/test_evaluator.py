"""Tests for :mod:`corlinman_goals.evaluator` (iter 6).

Per design test matrix:

- ``cascade_short_to_mid_aggregates_max`` — mid display = max(direct,
  avg(child shorts in window)).
- Long-tier two-number split: recent direct + trailing-4-week mid avg.

Plus tree-depth-3 coverage: long → mid → short with grandchildren
populated, scoring exercises the full forest.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from corlinman_goals.evaluator import (
    LONG_TRAILING_MID_WEEKS,
    aggregate_long,
    aggregate_mid,
)
from corlinman_goals.placeholders import NO_EVIDENCE_SENTINEL
from corlinman_goals.state import Goal, GoalEvaluation
from corlinman_goals.store import GoalStore
from corlinman_goals.windows import mid_window

_NOW = int(datetime(2026, 5, 9, 14, 0, tzinfo=UTC).timestamp() * 1000)
_HOUR_MS = 3600 * 1000
_DAY_MS = 24 * _HOUR_MS
_WEEK_MS = 7 * _DAY_MS


def _g(
    *,
    goal_id: str,
    tier: str,
    parent: str | None = None,
    body: str | None = None,
    agent_id: str = "mentor",
    created_at_ms: int | None = None,
) -> Goal:
    return Goal(
        id=goal_id,
        agent_id=agent_id,
        tier=tier,
        body=body or f"goal-{goal_id}",
        created_at_ms=created_at_ms if created_at_ms is not None else _NOW - 30 * _DAY_MS,
        target_date_ms=_NOW + 60 * _DAY_MS,
        parent_goal_id=parent,
        status="active",
        source="operator_cli",
    )


def _ev(
    *,
    goal_id: str,
    score: int,
    at_ms: int,
    narrative: str = "narr",
    run_id: str = "stub",
) -> GoalEvaluation:
    return GoalEvaluation(
        goal_id=goal_id,
        evaluated_at_ms=at_ms,
        score_0_to_10=score,
        narrative=narrative,
        evidence_episode_ids=[],
        reflection_run_id=run_id,
    )


@pytest.fixture
async def store(tmp_path: Path):
    s = await GoalStore.open_or_create(tmp_path / "agent_goals.sqlite")
    try:
        yield s
    finally:
        await s.close()


# ---------------------------------------------------------------------------
# aggregate_mid — max(direct, avg(children))
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mid_display_is_max_of_direct_and_children_avg(
    store: GoalStore,
) -> None:
    """direct=4, children=[6,8] → avg=7 → display=7 (max)."""
    week = mid_window(_NOW)
    mid = _g(goal_id="m", tier="mid")
    await store.insert_goal(mid)
    await store.insert_evaluation(
        _ev(goal_id="m", score=4, at_ms=week.end_ms - _HOUR_MS)
    )
    for cid, score in [("c1", 6), ("c2", 8)]:
        await store.insert_goal(_g(goal_id=cid, tier="short", parent="m"))
        await store.insert_evaluation(
            _ev(goal_id=cid, score=score, at_ms=week.start_ms + _DAY_MS)
        )

    result = await aggregate_mid(store=store, mid_goal=mid, window=week)

    assert result.direct_score == 4
    assert result.children_avg == 7.0
    assert result.display_score == 7.0
    assert sorted(result.contributing_child_ids) == ["c1", "c2"]


@pytest.mark.asyncio
async def test_mid_display_falls_back_to_direct_when_no_children(
    store: GoalStore,
) -> None:
    """Direct-only goal: display_score == direct."""
    week = mid_window(_NOW)
    mid = _g(goal_id="m", tier="mid")
    await store.insert_goal(mid)
    await store.insert_evaluation(_ev(goal_id="m", score=6, at_ms=week.end_ms))

    result = await aggregate_mid(store=store, mid_goal=mid, window=week)
    assert result.direct_score == 6
    assert result.children_avg is None
    assert result.display_score == 6.0


@pytest.mark.asyncio
async def test_mid_display_falls_back_to_children_when_direct_missing(
    store: GoalStore,
) -> None:
    """Mid never graded directly but children scored: display = avg.

    The design's ``max`` rule reduces to "whichever side has signal"
    when one side is None; this test pins that behaviour so a future
    refactor can't silently treat None as 0.
    """
    week = mid_window(_NOW)
    mid = _g(goal_id="m", tier="mid")
    await store.insert_goal(mid)
    for cid, score in [("c1", 5), ("c2", 9)]:
        await store.insert_goal(_g(goal_id=cid, tier="short", parent="m"))
        await store.insert_evaluation(
            _ev(goal_id=cid, score=score, at_ms=week.start_ms + _DAY_MS)
        )

    result = await aggregate_mid(store=store, mid_goal=mid, window=week)
    assert result.direct_score is None
    assert result.children_avg == 7.0
    assert result.display_score == 7.0


@pytest.mark.asyncio
async def test_mid_excludes_no_evidence_sentinel_rows(
    store: GoalStore,
) -> None:
    """Sentinel ``no_evidence`` rows must not pollute the aggregate.

    ``{{goals.failing}}`` already excludes them; the cascade has to
    match or operators see contradictory numbers between
    ``{{goals.weekly}}`` and ``{{goals.failing}}``.
    """
    week = mid_window(_NOW)
    mid = _g(goal_id="m", tier="mid")
    await store.insert_goal(mid)
    # Newest is sentinel; older real score should win for "direct".
    await store.insert_evaluation(
        _ev(
            goal_id="m",
            score=0,
            at_ms=week.end_ms,
            narrative=NO_EVIDENCE_SENTINEL,
        )
    )
    await store.insert_evaluation(
        _ev(goal_id="m", score=8, at_ms=week.end_ms - _DAY_MS)
    )

    result = await aggregate_mid(store=store, mid_goal=mid, window=week)
    assert result.direct_score == 8


@pytest.mark.asyncio
async def test_mid_window_filter_excludes_old_child_scores(
    store: GoalStore,
) -> None:
    """Child scored two weeks ago must not count toward "this week"
    average. Window enforcement is what makes a re-graded goal
    behave deterministically across tier boundaries."""
    week = mid_window(_NOW)
    mid = _g(goal_id="m", tier="mid")
    await store.insert_goal(mid)
    await store.insert_goal(_g(goal_id="c", tier="short", parent="m"))
    # In window:
    await store.insert_evaluation(
        _ev(goal_id="c", score=5, at_ms=week.start_ms + _HOUR_MS)
    )
    # Out of window (last week):
    await store.insert_evaluation(
        _ev(goal_id="c", score=10, at_ms=week.start_ms - _DAY_MS)
    )

    result = await aggregate_mid(store=store, mid_goal=mid, window=week)
    assert result.children_avg == 5.0
    assert result.contributing_child_ids == ["c"]


@pytest.mark.asyncio
async def test_aggregate_mid_rejects_wrong_tier(store: GoalStore) -> None:
    """Caller error guard — mismatched tier raises immediately."""
    long_goal = _g(goal_id="L", tier="long")
    await store.insert_goal(long_goal)
    with pytest.raises(ValueError, match="tier='mid'"):
        await aggregate_mid(
            store=store, mid_goal=long_goal, window=mid_window(_NOW)
        )


# ---------------------------------------------------------------------------
# aggregate_long — two numbers (recent direct + trailing 4w mid avg)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_two_number_surface(store: GoalStore) -> None:
    """Long with direct=5 and three mid children scored within the
    trailing 4 weeks: trailing_avg = mean of three scores; recent
    direct = 5."""
    long_goal = _g(goal_id="L", tier="long")
    await store.insert_goal(long_goal)
    await store.insert_evaluation(
        _ev(goal_id="L", score=5, at_ms=_NOW - _DAY_MS)
    )
    for i, score in enumerate([4, 7, 9]):
        cid = f"M{i}"
        await store.insert_goal(_g(goal_id=cid, tier="mid", parent="L"))
        await store.insert_evaluation(
            _ev(
                goal_id=cid,
                score=score,
                at_ms=_NOW - (i + 1) * _WEEK_MS + _HOUR_MS,
            )
        )

    result = await aggregate_long(
        store=store, long_goal=long_goal, now_ms=_NOW
    )
    assert result.recent_direct_score == 5
    assert result.trailing_mid_count == 3
    assert result.trailing_mid_avg == pytest.approx((4 + 7 + 9) / 3)
    assert sorted(result.contributing_mid_ids) == ["M0", "M1", "M2"]


@pytest.mark.asyncio
async def test_long_trailing_window_excludes_old_mid_scores(
    store: GoalStore,
) -> None:
    """Mid scored 5 weeks ago is outside the 4-week trailing window
    and must not contribute. The constant ``LONG_TRAILING_MID_WEEKS``
    is the dial; pinning the exclusion test against it catches
    accidental constant churn."""
    long_goal = _g(goal_id="L", tier="long")
    await store.insert_goal(long_goal)
    await store.insert_goal(_g(goal_id="M_in", tier="mid", parent="L"))
    await store.insert_goal(_g(goal_id="M_old", tier="mid", parent="L"))
    await store.insert_evaluation(
        _ev(goal_id="M_in", score=8, at_ms=_NOW - _WEEK_MS)
    )
    # 5 weeks ago: outside the 4-week window.
    await store.insert_evaluation(
        _ev(
            goal_id="M_old",
            score=10,
            at_ms=_NOW - (LONG_TRAILING_MID_WEEKS + 1) * _WEEK_MS,
        )
    )

    result = await aggregate_long(
        store=store, long_goal=long_goal, now_ms=_NOW
    )
    assert result.trailing_mid_avg == 8.0
    assert result.trailing_mid_count == 1
    assert result.contributing_mid_ids == ["M_in"]


@pytest.mark.asyncio
async def test_long_with_no_signals_returns_nones(store: GoalStore) -> None:
    """Brand-new long goal, no graded children, no own grade → both
    numbers are None and the count is zero. Display layer renders
    "—" rather than "0" for this case (asserts the row exists)."""
    long_goal = _g(goal_id="L", tier="long")
    await store.insert_goal(long_goal)

    result = await aggregate_long(
        store=store, long_goal=long_goal, now_ms=_NOW
    )
    assert result.recent_direct_score is None
    assert result.trailing_mid_avg is None
    assert result.trailing_mid_count == 0


@pytest.mark.asyncio
async def test_aggregate_long_rejects_wrong_tier(store: GoalStore) -> None:
    short_goal = _g(goal_id="s", tier="short")
    await store.insert_goal(short_goal)
    with pytest.raises(ValueError, match="tier='long'"):
        await aggregate_long(store=store, long_goal=short_goal, now_ms=_NOW)


# ---------------------------------------------------------------------------
# Tree depth 3 — long → mid → short, full forest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_three_tier_forest_aggregates_correctly(
    store: GoalStore,
) -> None:
    """End-to-end depth-3 walk: aggregate_mid against the mid tier
    sees children; aggregate_long against the long sees mids; the
    grandchild shorts contribute to mids' display, not directly to
    long.

    Forest:
        long L
        ├── mid M1
        │   ├── short S1a (score 6 in week)
        │   └── short S1b (score 8 in week)
        └── mid M2
            └── short S2a (score 4 in week)

    M1 direct=3 → display=max(3, avg(6,8))=7
    M2 direct=None → display=avg(4)=4
    L direct=None → trailing_mid_avg=avg(M1=3, M2=None) — only mids
    with in-window evaluations contribute. M2 is never graded directly,
    so trailing_mid_avg considers only M1 = 3.
    """
    week = mid_window(_NOW)

    long_goal = _g(goal_id="L", tier="long")
    await store.insert_goal(long_goal)
    for mid_id in ["M1", "M2"]:
        await store.insert_goal(_g(goal_id=mid_id, tier="mid", parent="L"))
    for short_id, parent_id in [
        ("S1a", "M1"),
        ("S1b", "M1"),
        ("S2a", "M2"),
    ]:
        await store.insert_goal(
            _g(goal_id=short_id, tier="short", parent=parent_id)
        )

    # Direct evaluation only on M1, scored just before _NOW so it
    # falls inside both the ISO week (for aggregate_mid) and the
    # 4-week trailing window (for aggregate_long).
    await store.insert_evaluation(
        _ev(goal_id="M1", score=3, at_ms=_NOW - _HOUR_MS)
    )
    # Short-tier evaluations in window.
    await store.insert_evaluation(
        _ev(goal_id="S1a", score=6, at_ms=week.start_ms + _DAY_MS)
    )
    await store.insert_evaluation(
        _ev(goal_id="S1b", score=8, at_ms=week.start_ms + _DAY_MS)
    )
    await store.insert_evaluation(
        _ev(goal_id="S2a", score=4, at_ms=week.start_ms + _DAY_MS)
    )

    m1 = await store.get_goal("M1")
    m2 = await store.get_goal("M2")
    assert m1 is not None
    assert m2 is not None

    m1_score = await aggregate_mid(store=store, mid_goal=m1, window=week)
    m2_score = await aggregate_mid(store=store, mid_goal=m2, window=week)

    assert m1_score.direct_score == 3
    assert m1_score.children_avg == 7.0
    assert m1_score.display_score == 7.0  # max(3, 7)
    assert sorted(m1_score.contributing_child_ids) == ["S1a", "S1b"]

    assert m2_score.direct_score is None
    assert m2_score.children_avg == 4.0
    assert m2_score.display_score == 4.0
    assert m2_score.contributing_child_ids == ["S2a"]

    # Long aggregation: only M1 has a stored direct evaluation, so
    # trailing_mid_avg considers only that score (the audit row is
    # the source of truth — design pins this).
    long_score = await aggregate_long(
        store=store, long_goal=long_goal, now_ms=_NOW
    )
    assert long_score.recent_direct_score is None
    assert long_score.trailing_mid_count == 1
    assert long_score.trailing_mid_avg == 3.0
    assert long_score.contributing_mid_ids == ["M1"]


@pytest.mark.asyncio
async def test_aggregator_skips_unrelated_tiers(store: GoalStore) -> None:
    """A long parent with a short grandchild (no mid in between) is
    a malformed forest the CLI rejects, but the aggregator still
    can't crash on it. The cascade only steps one tier per call —
    aggregate_long looks for mids, finds none, returns ``None`` avg."""
    long_goal = _g(goal_id="L", tier="long")
    await store.insert_goal(long_goal)
    # Short-tier child of a long would be rejected by the iter-7 CLI
    # but a direct DB writer could still create one. We test the
    # aggregator doesn't accidentally promote it.
    await store.insert_goal(_g(goal_id="S", tier="short", parent="L"))
    await store.insert_evaluation(_ev(goal_id="S", score=9, at_ms=_NOW))

    result = await aggregate_long(
        store=store, long_goal=long_goal, now_ms=_NOW
    )
    assert result.trailing_mid_count == 0
    assert result.trailing_mid_avg is None
