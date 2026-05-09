"""Cascade aggregation: short → mid → long parent-score derivation.

`parent_goal_id` makes the goal table a forest. The reflection job
(iter 5) writes per-goal scores; this module turns those rows into
the **display** scores operators and ``{{goals.*}}`` placeholders
read.

Two contracts the design pins (``§"Cascading"``):

- A ``mid`` goal's display score is ``max(direct_score, avg(child
  short scores in window))``. Max not weighted-avg — operators want
  optimistic surfacing; a strong week is a strong week even if
  Tuesday flopped.
- A ``long`` goal surfaces *two* numbers: most recent direct score
  AND trailing-4-week average of ``mid`` children. A single number
  hides the trend.
- ``{{goals.failing}}`` queries the **stored** ``score_0_to_10`` only,
  never the aggregate — the audit row is the source of truth.

We compute at read time (one query per tier, ≤ 50 rows) — no
materialised view. Tree depth is enforced at write time by the CLI's
``cli_set_rejects_cross_tier_parent`` (iter 7+) so this layer can
trust ``short.parent → mid.parent → long`` and never recurses past
two levels of descent. We still defend with a depth guard in case a
future direct-SQL writer creates a degenerate row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from corlinman_goals.state import NO_EVIDENCE_SENTINEL, Goal, GoalEvaluation
from corlinman_goals.store import DEFAULT_TENANT_ID, GoalStore
from corlinman_goals.windows import Window

# Trailing-4-week roll-up for long-tier display (the design's
# "trailing-4-week average of mid children"). Surfaced as a constant
# so test asserts and future tuning live in one spot.
LONG_TRAILING_MID_WEEKS: Final[int] = 4

# Hard guard against pathological parent chains. The CLI rejects
# cross-tier parents (short cannot parent mid; mid cannot parent
# long), so a legit forest is at most depth 3 (long → mid → short).
# A direct-SQL writer that bypassed the CLI could create deeper
# loops; the guard here aborts traversal at depth 4 with a clear
# error rather than recursing forever.
_MAX_CASCADE_DEPTH: Final[int] = 4

_MS_PER_SECOND: Final[int] = 1000
_SECONDS_PER_DAY: Final[int] = 86_400
_WEEK_MS: Final[int] = 7 * _SECONDS_PER_DAY * _MS_PER_SECOND


@dataclass(frozen=True)
class MidScore:
    """Aggregated display score for one mid-tier goal.

    ``direct_score`` is the most recent reflection score on the goal
    itself (``None`` if never graded or only sentinel rows).
    ``children_avg`` is the mean of child short-tier scores within
    ``window`` (``None`` if no children or no scored children in
    window). ``display_score`` is the design's ``max(direct,
    children_avg)``, falling back to whichever side is non-None.

    All three live on the row so a debug / admin surface can show
    *why* a number ended up where it did.
    """

    goal_id: str
    direct_score: int | None
    children_avg: float | None
    display_score: float | None
    contributing_child_ids: list[str]


@dataclass(frozen=True)
class LongScore:
    """Aggregated display surface for one long-tier goal.

    The design calls for "two numbers" so the operator sees the
    trend, not just a point estimate. ``recent_direct_score`` is the
    long goal's own latest reflection; ``trailing_mid_avg`` is the
    average of mid children's most-recent scores within the
    trailing window.
    """

    goal_id: str
    recent_direct_score: int | None
    trailing_mid_avg: float | None
    trailing_mid_count: int
    contributing_mid_ids: list[str]


def _latest_signal_score(evaluations: list[GoalEvaluation]) -> int | None:
    """Most recent score whose narrative is **not** the no-evidence
    sentinel. Returns ``None`` if every row is sentinel-only.

    Mirrors the placeholder resolver's exclusion rule — sentinel rows
    are "no activity", not "score zero", and folding them into an
    aggregate would tank a healthy goal that simply hadn't generated
    any episodes that week.
    """
    for ev in evaluations:
        if ev.narrative == NO_EVIDENCE_SENTINEL:
            continue
        return ev.score_0_to_10
    return None


def _eval_in_window(
    evaluations: list[GoalEvaluation], *, window: Window
) -> GoalEvaluation | None:
    """Most recent non-sentinel evaluation whose ``evaluated_at`` is
    inside ``[start_ms, end_ms]`` (inclusive on both ends).

    Inclusive upper because the reflection job pins ``evaluated_at =
    window.end_ms`` — a half-open ``< end`` would silently exclude
    the most-recent row, which is exactly the row this function is
    here to find.
    """
    for ev in evaluations:
        if ev.narrative == NO_EVIDENCE_SENTINEL:
            continue
        if window.start_ms <= ev.evaluated_at_ms <= window.end_ms:
            return ev
    return None


async def aggregate_mid(
    *,
    store: GoalStore,
    mid_goal: Goal,
    window: Window,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> MidScore:
    """Compute the display score for one mid-tier goal.

    ``window`` is the reflection window the operator cares about
    (typically ``mid_window(now_ms)``). The mid's own evaluations are
    walked most-recent-first; child short evaluations must fall
    inside ``window`` to count, so the same goal evaluated against
    "this week" vs "last week" produces stable numbers per
    snapshot.

    Returns :class:`MidScore` even when both sides are ``None`` —
    callers who need to render "no data yet" see the row, not a
    swallowed exception.
    """
    if mid_goal.tier != "mid":
        raise ValueError(
            f"aggregate_mid expects tier='mid', got {mid_goal.tier!r}"
        )

    direct_evals = await store.list_evaluations(mid_goal.id)
    direct_score = _latest_signal_score(direct_evals)

    # Pull child short goals (any status — operators may have
    # archived a child mid-week and we still want the score it
    # earned to count).
    children = await _list_children(
        store=store,
        parent_id=mid_goal.id,
        tenant_id=tenant_id,
        depth=1,
    )
    short_children = [c for c in children if c.tier == "short"]

    child_scores: list[int] = []
    contributing: list[str] = []
    for child in short_children:
        evs = await store.list_evaluations(child.id)
        ev = _eval_in_window(evs, window=window)
        if ev is None:
            continue
        child_scores.append(ev.score_0_to_10)
        contributing.append(child.id)

    children_avg = (
        sum(child_scores) / len(child_scores) if child_scores else None
    )
    display = _max_optional(
        None if direct_score is None else float(direct_score),
        children_avg,
    )
    return MidScore(
        goal_id=mid_goal.id,
        direct_score=direct_score,
        children_avg=children_avg,
        display_score=display,
        contributing_child_ids=contributing,
    )


async def aggregate_long(
    *,
    store: GoalStore,
    long_goal: Goal,
    now_ms: int,
    tenant_id: str = DEFAULT_TENANT_ID,
    trailing_weeks: int = LONG_TRAILING_MID_WEEKS,
) -> LongScore:
    """Compute the two-number display for one long-tier goal.

    The trailing window for mid children is ``[now - trailing_weeks
    * 7d, now]``. We pull the most-recent in-window non-sentinel
    score per child mid (one number per child, not all rows) so a
    chatty mid that got graded twice in a week doesn't double-weight
    the average.
    """
    if long_goal.tier != "long":
        raise ValueError(
            f"aggregate_long expects tier='long', got {long_goal.tier!r}"
        )

    direct_evals = await store.list_evaluations(long_goal.id)
    recent_direct = _latest_signal_score(direct_evals)

    children = await _list_children(
        store=store,
        parent_id=long_goal.id,
        tenant_id=tenant_id,
        depth=1,
    )
    mid_children = [c for c in children if c.tier == "mid"]

    trailing_window = Window(
        start_ms=now_ms - trailing_weeks * _WEEK_MS,
        end_ms=now_ms,
    )
    mid_scores: list[int] = []
    contributing: list[str] = []
    for mid in mid_children:
        evs = await store.list_evaluations(mid.id)
        ev = _eval_in_window(evs, window=trailing_window)
        if ev is None:
            continue
        mid_scores.append(ev.score_0_to_10)
        contributing.append(mid.id)

    avg = sum(mid_scores) / len(mid_scores) if mid_scores else None
    return LongScore(
        goal_id=long_goal.id,
        recent_direct_score=recent_direct,
        trailing_mid_avg=avg,
        trailing_mid_count=len(mid_scores),
        contributing_mid_ids=contributing,
    )


async def _list_children(
    *,
    store: GoalStore,
    parent_id: str,
    tenant_id: str,
    depth: int,
) -> list[Goal]:
    """Direct children of ``parent_id`` (one level only).

    Reusable across :func:`aggregate_mid` (children = short) and
    :func:`aggregate_long` (children = mid). The depth guard is
    paranoid defence: ``GoalStore`` doesn't currently support a
    ``parent_goal_id =`` filter, so we list-all-then-filter — fine at
    the design's "≤ 50 rows per tenant" scale, would need an index
    pass at a few thousand rows.
    """
    if depth > _MAX_CASCADE_DEPTH:
        raise RuntimeError(
            f"cascade depth exceeded for parent={parent_id!r} — "
            "likely a parent-cycle from a direct-SQL writer."
        )
    all_goals = await store.list_goals(tenant_id=tenant_id)
    return [g for g in all_goals if g.parent_goal_id == parent_id]


def _max_optional(a: float | None, b: float | None) -> float | None:
    """Maximum of two ``Optional[float]`` values.

    ``max(None, x) == x``; ``max(None, None) is None``. Built-in
    ``max`` doesn't handle ``None`` cleanly, so we spell it out — the
    aggregate display value is allowed to be ``None`` (no signal yet)
    and we don't want to silently treat that as zero.
    """
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


__all__ = [
    "LONG_TRAILING_MID_WEEKS",
    "LongScore",
    "MidScore",
    "aggregate_long",
    "aggregate_mid",
]
