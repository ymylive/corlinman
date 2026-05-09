"""Tests for ``{{goals.weekly}}`` cascade-aware aggregation (iter 8).

The iter-3 placeholder showed only direct evaluations; iter 8 folds
:func:`corlinman_goals.evaluator.aggregate_mid` into the resolver so a
mid goal whose direct score is missing or low surfaces the average of
its child shorts when that's higher.

Three scenarios pin the contract:

- ``cascade_lifts_weekly_when_no_direct_score`` — a mid with no direct
  evaluation but two child shorts with scores 7 and 9 surfaces the
  cascade average (8.0).
- ``cascade_takes_max_over_direct`` — direct=4, children-avg=8 →
  display=8 (design's ``max(direct, avg)``).
- ``direct_score_wins_when_higher`` — direct=9, children-avg=5 →
  scored bullet still uses the direct narrative (cascade doesn't
  shadow a strong direct signal).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from corlinman_goals.placeholders import GoalsResolver
from corlinman_goals.state import Goal, GoalEvaluation
from corlinman_goals.store import GoalStore

_NOW = int(datetime(2026, 5, 9, 14, 0, tzinfo=UTC).timestamp() * 1000)
_DAY_MS = 86_400 * 1000
_WEEK_MS = 7 * _DAY_MS

# Last week's window — the same range ``_resolve_weekly`` uses for both
# the most-recent-eval lookup and the cascade aggregation.
_LAST_WEEK_LO = _NOW - 2 * _WEEK_MS
_LAST_WEEK_HI = _NOW - _WEEK_MS
# Pick an evaluated_at inside the previous-week range so the
# evaluator's ``_eval_in_window`` (inclusive on both ends) catches it.
_EVAL_AT = _NOW - 8 * _DAY_MS


@pytest.fixture(autouse=True)
def _freeze_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("corlinman_goals.placeholders._now_ms", lambda: _NOW)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "agent_goals.sqlite"


def _g(
    *,
    gid: str,
    tier: str,
    body: str,
    parent: str | None = None,
) -> Goal:
    return Goal(
        id=gid,
        agent_id="mentor",
        tier=tier,
        body=body,
        created_at_ms=_NOW - 30 * _DAY_MS,
        target_date_ms=_NOW + _DAY_MS,
        parent_goal_id=parent,
        status="active",
        source="operator_cli",
    )


def _ev(*, gid: str, score: int, narrative: str) -> GoalEvaluation:
    return GoalEvaluation(
        goal_id=gid,
        evaluated_at_ms=_EVAL_AT,
        score_0_to_10=score,
        narrative=narrative,
        evidence_episode_ids=[],
        reflection_run_id=f"r-{gid}",
    )


@pytest.mark.asyncio
async def test_cascade_lifts_weekly_when_no_direct_score(db_path: Path) -> None:
    """Mid with no direct eval but child shorts → cascade fills in.

    Two child shorts with scores 7 and 9 → average 8.0; the placeholder
    should surface that as the displayed score, with a synthesised
    narrative.
    """
    async with GoalStore(db_path) as store:
        await store.insert_goal(_g(gid="m-1", tier="mid", body="Deepen db skills"))
        await store.insert_goal(_g(gid="s-1", tier="short", body="postgres", parent="m-1"))
        await store.insert_goal(_g(gid="s-2", tier="short", body="mysql", parent="m-1"))
        await store.insert_evaluation(_ev(gid="s-1", score=7, narrative="okay"))
        await store.insert_evaluation(_ev(gid="s-2", score=9, narrative="great"))

        resolver = GoalsResolver(store)
        out = await resolver.resolve("weekly", "mentor")

    assert "Deepen db skills" in out
    assert "score 8" in out
    # Synthesised narrative because the mid had no direct eval.
    assert "via 2 child goals" in out


@pytest.mark.asyncio
async def test_cascade_takes_max_over_direct(db_path: Path) -> None:
    """Direct=4, children-avg=8 → cascade wins, display=8."""
    async with GoalStore(db_path) as store:
        await store.insert_goal(_g(gid="m-1", tier="mid", body="Deepen infra"))
        await store.insert_goal(_g(gid="s-1", tier="short", body="x", parent="m-1"))
        await store.insert_goal(_g(gid="s-2", tier="short", body="y", parent="m-1"))
        await store.insert_evaluation(_ev(gid="m-1", score=4, narrative="meh"))
        await store.insert_evaluation(_ev(gid="s-1", score=7, narrative="ok"))
        await store.insert_evaluation(_ev(gid="s-2", score=9, narrative="strong"))

        resolver = GoalsResolver(store)
        out = await resolver.resolve("weekly", "mentor")

    # Cascade beats direct (8 > 4) — the placeholder uses cascade
    # display + the direct narrative (the operator-readable string is
    # still about the mid goal, not synthesised).
    assert "score 8" in out
    assert "meh" in out


@pytest.mark.asyncio
async def test_direct_score_wins_when_higher(db_path: Path) -> None:
    """Direct=9, children-avg=5 → display=9 (direct's narrative)."""
    async with GoalStore(db_path) as store:
        await store.insert_goal(_g(gid="m-1", tier="mid", body="Deepen rust"))
        await store.insert_goal(_g(gid="s-1", tier="short", body="x", parent="m-1"))
        await store.insert_goal(_g(gid="s-2", tier="short", body="y", parent="m-1"))
        await store.insert_evaluation(_ev(gid="m-1", score=9, narrative="excellent"))
        await store.insert_evaluation(_ev(gid="s-1", score=4, narrative="meh"))
        await store.insert_evaluation(_ev(gid="s-2", score=6, narrative="okay"))

        resolver = GoalsResolver(store)
        out = await resolver.resolve("weekly", "mentor")

    # Direct=9 wins over children-avg=5; the placeholder emits the
    # direct scored bullet (no fractional cascade output).
    assert "score 9" in out
    assert "excellent" in out


@pytest.mark.asyncio
async def test_cascade_with_no_direct_and_no_children_falls_back_to_bare(db_path: Path) -> None:
    """Empty cascade + no direct → bare bullet (existing iter-3 behaviour)."""
    async with GoalStore(db_path) as store:
        await store.insert_goal(_g(gid="m-1", tier="mid", body="Empty mid"))
        resolver = GoalsResolver(store)
        out = await resolver.resolve("weekly", "mentor")
    assert out == "- Empty mid"
