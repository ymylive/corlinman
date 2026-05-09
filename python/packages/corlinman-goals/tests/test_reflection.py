"""Tests for :mod:`corlinman_goals.reflection` (iter 5).

Per design test matrix:

- ``reflection_idempotent_within_window`` — second call is a no-op.
- ``reflection_drops_hallucinated_episode_ids`` — cited but not in
  the input set are filtered.
- ``reflection_no_evidence_writes_sentinel`` — empty episode list →
  ``score=0, narrative='no_evidence'``, no LLM call.
- ``reflection_partial_window_for_new_goal`` — goal created
  Wednesday evaluated Sunday gets ``(created_at, window_end)``.

Plus the runner-loop guards: per-goal exception isolation, narrative
cap, mixed outcomes accounting.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from corlinman_goals.evidence import EvidenceEpisode, StaticEvidence
from corlinman_goals.placeholders import NO_EVIDENCE_SENTINEL
from corlinman_goals.reflection import (
    NARRATIVE_MAX_CHARS,
    GraderReply,
    make_callable_grader,
    make_constant_grader,
    reflect_once,
)
from corlinman_goals.state import Goal
from corlinman_goals.store import GoalStore
from corlinman_goals.windows import mid_window, short_window

# Saturday afternoon; Sunday is window end-of-week for the ``mid``
# tests since the ISO week starts on the previous Monday.
_NOW = int(datetime(2026, 5, 9, 14, 0, tzinfo=UTC).timestamp() * 1000)
_HOUR_MS = 3600 * 1000
_DAY_MS = 24 * _HOUR_MS


def _g(
    *,
    goal_id: str,
    tier: str = "short",
    body: str = "do thing",
    created_at_ms: int | None = None,
    target_date_ms: int | None = None,
    agent_id: str = "mentor",
) -> Goal:
    """Goal builder with sensible defaults for window math.

    ``created_at_ms`` defaults to "yesterday" so the goal is inside
    every short/mid/long window without polluting partial-window
    tests; partial-window tests pass ``created_at_ms`` explicitly.
    """
    return Goal(
        id=goal_id,
        agent_id=agent_id,
        tier=tier,
        body=body,
        created_at_ms=created_at_ms if created_at_ms is not None else _NOW - _DAY_MS,
        target_date_ms=target_date_ms if target_date_ms is not None else _NOW + _DAY_MS,
        status="active",
        source="operator_cli",
    )


def _ev(*, episode_id: str, started: int, ended: int, body: str = "x") -> EvidenceEpisode:
    return EvidenceEpisode(
        episode_id=episode_id,
        started_at_ms=started,
        ended_at_ms=ended,
        kind="conversation",
        summary_text=body,
        importance_score=0.5,
    )


@pytest.fixture
async def store(tmp_path: Path):
    s = await GoalStore.open_or_create(tmp_path / "agent_goals.sqlite")
    try:
        yield s
    finally:
        await s.close()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflection_writes_evaluation_for_active_goal(store: GoalStore) -> None:
    """One active goal + one evidence episode → one ``goal_evaluations`` row.

    The PK we expect: ``(goal_id, evaluated_at_ms = window.end_ms)``.
    Pinning ``evaluated_at`` to ``window_end`` is what makes the
    idempotency test below a no-op on rerun.
    """
    goal = _g(goal_id="g-1", tier="short")
    await store.insert_goal(goal)
    evidence = StaticEvidence(
        [_ev(episode_id="e-1", started=_NOW - 6 * _HOUR_MS, ended=_NOW - 5 * _HOUR_MS)]
    )

    summary = await reflect_once(
        store=store,
        evidence_source=evidence,
        grader=make_constant_grader(score=7, narrative="solid week"),
        tier="short",
        agent_id="mentor",
        now_ms=_NOW,
    )

    assert summary.goals_total == 1
    assert summary.goals_scored == 1
    assert summary.goals_no_evidence == 0
    expected_window = short_window(_NOW)
    rows = await store.list_evaluations(goal.id)
    assert len(rows) == 1
    assert rows[0].score_0_to_10 == 7
    assert rows[0].narrative == "solid week"
    assert rows[0].evidence_episode_ids == ["e-1"]
    assert rows[0].evaluated_at_ms == expected_window.end_ms
    assert rows[0].reflection_run_id == f"short-{expected_window.start_ms}"


# ---------------------------------------------------------------------------
# reflection_idempotent_within_window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflection_idempotent_within_window(store: GoalStore) -> None:
    """Two reflect_once calls with the same ``now`` and same evidence
    must produce exactly one row. Test pins ``score=7`` on the first
    call and ``score=2`` on the second; if the second wrote, the
    score would change."""
    goal = _g(goal_id="g-1", tier="short")
    await store.insert_goal(goal)
    evidence = StaticEvidence(
        [_ev(episode_id="e-1", started=_NOW - 2 * _HOUR_MS, ended=_NOW - _HOUR_MS)]
    )

    await reflect_once(
        store=store,
        evidence_source=evidence,
        grader=make_constant_grader(score=7),
        tier="short",
        agent_id="mentor",
        now_ms=_NOW,
    )
    second = await reflect_once(
        store=store,
        evidence_source=evidence,
        grader=make_constant_grader(score=2),
        tier="short",
        agent_id="mentor",
        now_ms=_NOW,
    )

    assert second.goals_skipped_idempotent == 1
    assert second.goals_scored == 0
    rows = await store.list_evaluations(goal.id)
    assert len(rows) == 1
    assert rows[0].score_0_to_10 == 7  # original survived


# ---------------------------------------------------------------------------
# reflection_drops_hallucinated_episode_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflection_drops_hallucinated_episode_ids(store: GoalStore) -> None:
    """The grader cites ``"made-up"`` plus a real id. Only the real
    one gets persisted; the run still succeeds (we don't fail the
    whole pass on a hallucination)."""
    await store.insert_goal(_g(goal_id="g-1", tier="short"))
    real = _ev(episode_id="real-ep", started=_NOW - _HOUR_MS, ended=_NOW)
    evidence = StaticEvidence([real])

    async def lying_grader(goal, window, evs):
        del goal, window, evs
        return GraderReply(
            score_0_to_10=6,
            narrative="cited a ghost",
            cited_episode_ids=["made-up", "real-ep", "another-ghost"],
        )

    await reflect_once(
        store=store,
        evidence_source=evidence,
        grader=make_callable_grader(lying_grader),
        tier="short",
        agent_id="mentor",
        now_ms=_NOW,
    )

    rows = await store.list_evaluations("g-1")
    assert rows[0].evidence_episode_ids == ["real-ep"]


# ---------------------------------------------------------------------------
# reflection_no_evidence_writes_sentinel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflection_no_evidence_writes_sentinel(store: GoalStore) -> None:
    """Empty episode list → sentinel row, no LLM call. The grader is
    a tripwire: if it's invoked the test fails, proving the runner
    short-circuits before paying for inference."""
    await store.insert_goal(_g(goal_id="g-1", tier="short"))
    evidence = StaticEvidence([])

    grader_called = []

    async def tripwire(goal, window, evs):
        grader_called.append(goal.id)
        return GraderReply(score_0_to_10=10, narrative="should not run", cited_episode_ids=[])

    summary = await reflect_once(
        store=store,
        evidence_source=evidence,
        grader=make_callable_grader(tripwire),
        tier="short",
        agent_id="mentor",
        now_ms=_NOW,
    )

    assert grader_called == []
    assert summary.goals_no_evidence == 1
    rows = await store.list_evaluations("g-1")
    assert rows[0].score_0_to_10 == 0
    assert rows[0].narrative == NO_EVIDENCE_SENTINEL
    assert rows[0].evidence_episode_ids == []


# ---------------------------------------------------------------------------
# reflection_partial_window_for_new_goal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflection_partial_window_for_new_goal(store: GoalStore) -> None:
    """Goal created Wednesday, evaluated Sunday on the mid tier.

    The window passed to the grader must start at ``created_at_ms``
    (Wednesday), not the ISO week's Monday — the design's "fair
    sample of available evidence" contract.
    """
    week = mid_window(_NOW)  # Mon..Mon
    # Wednesday = Monday + 2 days.
    wed_created = week.start_ms + 2 * _DAY_MS
    goal = _g(
        goal_id="g-mid", tier="mid", created_at_ms=wed_created
    )
    await store.insert_goal(goal)

    seen_window = []

    async def spy(goal, window, evs):
        seen_window.append(window)
        return GraderReply(score_0_to_10=5, narrative="ok", cited_episode_ids=[])

    evidence = StaticEvidence(
        [
            _ev(
                episode_id="e-thu",
                started=wed_created + _DAY_MS,
                ended=wed_created + _DAY_MS + _HOUR_MS,
            ),
        ]
    )
    await reflect_once(
        store=store,
        evidence_source=evidence,
        grader=make_callable_grader(spy),
        tier="mid",
        agent_id="mentor",
        now_ms=_NOW,
    )

    assert len(seen_window) == 1
    w = seen_window[0]
    # Lower bound clamped up to created_at; upper bound still the ISO
    # week's end (next Monday midnight UTC).
    assert w.start_ms == wed_created
    assert w.end_ms == week.end_ms


# ---------------------------------------------------------------------------
# Per-goal exception isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_failing_goal_does_not_strand_others(store: GoalStore) -> None:
    """Two goals; the grader raises on the first, succeeds on the
    second. Run continues, summary counts the failure, the second
    goal still gets a row."""
    await store.insert_goal(_g(goal_id="bad", tier="short"))
    await store.insert_goal(_g(goal_id="good", tier="short", body="other"))
    evidence = StaticEvidence(
        [_ev(episode_id="e-1", started=_NOW - _HOUR_MS, ended=_NOW)]
    )

    async def picky(goal, window, evs):
        if goal.id == "bad":
            raise RuntimeError("LLM 500")
        return GraderReply(score_0_to_10=8, narrative="fine", cited_episode_ids=["e-1"])

    summary = await reflect_once(
        store=store,
        evidence_source=evidence,
        grader=make_callable_grader(picky),
        tier="short",
        agent_id="mentor",
        now_ms=_NOW,
    )

    assert summary.goals_failed == 1
    assert summary.failed_goal_ids == ["bad"]
    assert summary.goals_scored == 1
    assert (await store.list_evaluations("bad")) == []
    assert (await store.list_evaluations("good"))[0].score_0_to_10 == 8


# ---------------------------------------------------------------------------
# Narrative cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narrative_truncated_to_cap(store: GoalStore) -> None:
    """Grader returns 500 chars → row stores ≤ 280 with a trailing ellipsis.

    Cap matches the design's ``narrative_max_chars`` so
    ``{{goals.weekly}}`` never inlines a runaway string.
    """
    await store.insert_goal(_g(goal_id="g-1", tier="short"))
    evidence = StaticEvidence(
        [_ev(episode_id="e-1", started=_NOW - _HOUR_MS, ended=_NOW)]
    )
    long_text = "abcde " * 100  # 600 chars

    await reflect_once(
        store=store,
        evidence_source=evidence,
        grader=make_constant_grader(score=4, narrative=long_text),
        tier="short",
        agent_id="mentor",
        now_ms=_NOW,
    )

    row = (await store.list_evaluations("g-1"))[0]
    assert len(row.narrative) <= NARRATIVE_MAX_CHARS
    assert row.narrative.endswith("…")


# ---------------------------------------------------------------------------
# No active goals — empty run accounting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_active_goals_returns_empty_summary(store: GoalStore) -> None:
    """A tier with zero active goals must not blow up window math
    (which dereferenced ``active[0]`` in the run-id derivation)."""
    summary = await reflect_once(
        store=store,
        evidence_source=StaticEvidence([]),
        grader=make_constant_grader(score=0),
        tier="short",
        agent_id="mentor",
        now_ms=_NOW,
    )
    assert summary.goals_total == 0
    assert summary.goals_scored == 0
    assert summary.failed_goal_ids == []


@pytest.mark.asyncio
async def test_unknown_tier_raises(store: GoalStore) -> None:
    with pytest.raises(ValueError, match="unknown tier"):
        await reflect_once(
            store=store,
            evidence_source=StaticEvidence([]),
            grader=make_constant_grader(score=0),
            tier="forever",
            agent_id="mentor",
            now_ms=_NOW,
        )
