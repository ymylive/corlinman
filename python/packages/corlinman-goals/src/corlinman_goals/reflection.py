"""Reflection job — grade goals against D1 episodes via an LLM.

One scheduled run per tier (cron rows in the design's config block):
short at 00:05 UTC, mid at Mon 00:10 UTC, long at 00:15 UTC on the
first of Jan/Apr/Jul/Oct. Each run picks active goals at the
requested tier, builds an evidence window from
:mod:`corlinman_goals.windows`, asks the grader for a 0-10 score +
narrative + cited ids, and writes one ``goal_evaluations`` row per
goal.

This iter wires the runner with a **stub** grader (callable
protocol + a deterministic test impl). Iter 7 swaps the stub for the
real ``corlinman-providers``-backed cheap-LLM call and PII redactor;
the runner contract here is the same one that final wiring drops
into.

Idempotency, retries, partial windows, no-evidence — all handled in
this layer per the design's "Idempotency, retries, partial-window"
section. The grader has one job: take a built prompt, return a
typed reply. Everything else is policy and lives here.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final, Protocol

from corlinman_goals.evidence import (
    DEFAULT_EVIDENCE_LIMIT,
    EpisodeEvidence,
    EvidenceEpisode,
)
from corlinman_goals.placeholders import NO_EVIDENCE_SENTINEL
from corlinman_goals.state import Goal, GoalEvaluation
from corlinman_goals.store import GoalStore
from corlinman_goals.windows import (
    Window,
    long_window,
    mid_window,
    short_window,
)

logger = logging.getLogger(__name__)

# Cap the narrative length we accept from the grader. The schema is
# unbounded TEXT but the design's ``narrative_max_chars = 280`` (one
# tweet) keeps prompt budgets sane downstream where placeholders
# inline these strings.
NARRATIVE_MAX_CHARS: Final[int] = 280

# Tier → reflection-window builder. Short and mid take only ``now``;
# long is per-goal (``created_at + 90d``) so the dispatcher passes
# the goal in for that tier. Encapsulating the shape here keeps the
# tier-window contract one switch deep.
TIER_TO_WINDOW: Final[dict[str, str]] = {
    "short": "rolling_24h",
    "mid": "iso_week",
    "long": "per_goal_90d",
}


@dataclass(frozen=True)
class GraderReply:
    """Typed reply from an LLM grader.

    Frozen so a misbehaving grader implementation can't mutate the
    reply between callsites (the runner double-checks
    ``cited_episode_ids`` against the input set; mutation would defeat
    that guard).
    """

    score_0_to_10: int
    narrative: str
    cited_episode_ids: list[str]


class Grader(Protocol):
    """Callable contract for the reflection grader.

    The runner doesn't care whether the implementation calls a remote
    LLM, a local model, or returns a constant — just that it takes a
    goal + evidence and returns a :class:`GraderReply`. This is the
    one seam iter 7 will swap.

    Implementations must be safe to call concurrently with different
    goals; the runner currently calls them serially for retry
    accounting, but that's a runner choice not a grader contract.
    """

    async def __call__(
        self,
        *,
        goal: Goal,
        window: Window,
        evidence: list[EvidenceEpisode],
    ) -> GraderReply:
        ...


# ---------------------------------------------------------------------------
# Built-in graders for tests + dev
# ---------------------------------------------------------------------------


def make_constant_grader(
    *, score: int, narrative: str = "stub"
) -> Grader:
    """Return a grader that ignores inputs and replies with a fixed
    score, citing every episode it was shown.

    Used in tests to assert downstream policy (idempotency, hallucination
    filter, sentinel) without LLM noise.
    """

    async def _grade(
        *,
        goal: Goal,
        window: Window,
        evidence: list[EvidenceEpisode],
    ) -> GraderReply:
        del goal, window
        return GraderReply(
            score_0_to_10=int(score),
            narrative=narrative,
            cited_episode_ids=[e.episode_id for e in evidence],
        )

    return _grade


def make_callable_grader(
    fn: Callable[
        [Goal, Window, list[EvidenceEpisode]],
        Awaitable[GraderReply],
    ],
) -> Grader:
    """Adapter from a positional-arg async fn to the kwargs-only
    :class:`Grader` shape. Useful for tests that want to spy on call
    arguments without re-spelling the protocol."""

    async def _grade(
        *,
        goal: Goal,
        window: Window,
        evidence: list[EvidenceEpisode],
    ) -> GraderReply:
        return await fn(goal, window, evidence)

    return _grade


# ---------------------------------------------------------------------------
# Run summary
# ---------------------------------------------------------------------------


@dataclass
class ReflectionSummary:
    """Per-tier run accounting.

    Counts not aggregated rows because two operators reading the run's
    log want to know *which* goals failed, not just "5 errors". The
    counts are also surfaced as Prometheus labels in iter 7+
    (``goals_reflection_total{outcome=...}``).
    """

    tier: str
    reflection_run_id: str
    window: Window
    goals_total: int = 0
    goals_scored: int = 0
    goals_no_evidence: int = 0
    goals_skipped_idempotent: int = 0
    goals_failed: int = 0
    failed_goal_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """Indirection so tests can monkeypatch ``time.time`` at module
    scope. Same shape as the placeholders module."""
    return int(time.time() * 1000)


def _build_window(
    *,
    tier: str,
    goal: Goal,
    now_ms: int,
) -> Window:
    """Tier → reflection window. Long is per-goal (``created_at +
    90d``); short/mid are now-anchored.

    Partial-window contract (design §"Partial windows"): a goal
    created mid-window is graded against ``(created_at, window_end)``,
    not the raw window. Implemented by clamping ``start_ms`` upward
    to the goal's ``created_at_ms`` after the tier-window pick. This
    means a Wednesday-authored goal evaluated Sunday gets four days
    of evidence, not seven; the score is honest about how much
    behaviour the agent had to demonstrate.
    """
    if tier == "short":
        base = short_window(now_ms)
    elif tier == "mid":
        base = mid_window(now_ms)
    elif tier == "long":
        base = long_window(now_ms, goal.created_at_ms)
    else:
        raise ValueError(f"unknown tier {tier!r}")
    if goal.created_at_ms > base.start_ms:
        return Window(
            start_ms=goal.created_at_ms,
            end_ms=base.end_ms,
        )
    return base


def _reflection_run_id(*, tier: str, window: Window) -> str:
    """``<tier>-<window_start_ms>`` — the design's idempotency key.

    Two runs that picked the same tier on the same wall-clock day
    derive the same id and the second's ``INSERT OR IGNORE`` is a
    no-op. Embedding the start instead of "today's date" means a
    long-tier run on April 1 and one on July 1 carry distinct ids
    even though both fired from the quarterly cron.
    """
    return f"{tier}-{int(window.start_ms)}"


def _evaluated_at_ms(window: Window) -> int:
    """When the evaluation row claims it happened — pinned to
    ``window.end_ms`` so two reruns of the same window write the same
    primary-key value, hitting the ``INSERT OR IGNORE`` guard.

    Using wall-clock ``now`` here would make every retry write a new
    row (different ``evaluated_at`` ⇒ different PK), defeating the
    idempotency contract. The PK is ``(goal_id, evaluated_at)``;
    ``evaluated_at = window_end`` makes that "one row per goal per
    window" by construction.
    """
    return int(window.end_ms)


def _filter_hallucinated_ids(
    cited: list[str], *, allowed: list[str]
) -> list[str]:
    """Intersect ``cited`` with ``allowed``, preserving cited order.

    The grader is allowed to cite a subset; what we never want to
    persist is an id the LLM made up (no episode exists). Dropping is
    safer than failing the run — the score still has signal even if
    a couple of ids dangled.
    """
    allowed_set = set(allowed)
    return [c for c in cited if c in allowed_set]


def _truncate_narrative(narrative: str) -> str:
    """Hard-cap to :data:`NARRATIVE_MAX_CHARS`.

    The schema accepts unbounded TEXT; the cap is a placeholder-
    rendering concern (``{{goals.weekly}}`` inlines these strings).
    Truncate at character boundary, append the ellipsis only if we
    actually clipped.
    """
    s = narrative.strip()
    if len(s) <= NARRATIVE_MAX_CHARS:
        return s
    return s[: NARRATIVE_MAX_CHARS - 1].rstrip() + "…"


async def reflect_once(
    *,
    store: GoalStore,
    evidence_source: EpisodeEvidence,
    grader: Grader,
    tier: str,
    agent_id: str,
    tenant_id: str = "default",
    now_ms: int | None = None,
    evidence_limit: int = DEFAULT_EVIDENCE_LIMIT,
) -> ReflectionSummary:
    """Run one reflection pass for ``tier`` and ``agent_id``.

    Walks every active goal at the tier, builds the evidence window,
    fetches episodes from D1, calls the grader (or skips it on empty
    evidence), filters hallucinated ids, and writes one
    ``goal_evaluations`` row per goal. Per-goal exceptions are
    counted, not raised — one bad goal must not strand the rest of
    the run.

    Returns a :class:`ReflectionSummary` so callers (CLI in iter 7+,
    the eventual scheduler subprocess) can log + emit metrics without
    parsing the SQLite afterwards.

    The ``now_ms`` kwarg is for tests; production callers pass nothing
    and the runner reads :func:`_now_ms` (which uses ``time.time``).
    """
    if tier not in TIER_TO_WINDOW:
        raise ValueError(f"unknown tier {tier!r}")
    now = now_ms if now_ms is not None else _now_ms()

    active = await store.list_goals(
        agent_id=agent_id,
        tier=tier,
        status="active",
        tenant_id=tenant_id,
    )

    # Use the first goal's window for the run-level id when the tier is
    # not ``long`` (short/mid windows are agent-global). For long,
    # every goal carries its own window so the run-level id is just a
    # convenience — we synthesise one from ``now`` as the "session"
    # id but every per-goal write uses its own derived id.
    if active and tier != "long":
        run_window = _build_window(tier=tier, goal=active[0], now_ms=now)
        run_id = _reflection_run_id(tier=tier, window=run_window)
    else:
        run_window = Window(start_ms=now, end_ms=now)
        run_id = f"{tier}-{now}"

    summary = ReflectionSummary(
        tier=tier,
        reflection_run_id=run_id,
        window=run_window,
    )
    summary.goals_total = len(active)

    for goal in active:
        try:
            wrote = await _reflect_one_goal(
                store=store,
                evidence_source=evidence_source,
                grader=grader,
                goal=goal,
                tier=tier,
                agent_id=agent_id,
                now_ms=now,
                evidence_limit=evidence_limit,
            )
        except Exception:
            logger.exception(
                "reflection_failed goal_id=%s tier=%s", goal.id, tier
            )
            summary.goals_failed += 1
            summary.failed_goal_ids.append(goal.id)
            continue

        if wrote.no_evidence:
            summary.goals_no_evidence += 1
        elif wrote.skipped_idempotent:
            summary.goals_skipped_idempotent += 1
        else:
            summary.goals_scored += 1

    return summary


@dataclass(frozen=True)
class _PerGoalResult:
    no_evidence: bool
    skipped_idempotent: bool


async def _reflect_one_goal(
    *,
    store: GoalStore,
    evidence_source: EpisodeEvidence,
    grader: Grader,
    goal: Goal,
    tier: str,
    agent_id: str,
    now_ms: int,
    evidence_limit: int,
) -> _PerGoalResult:
    """Grade one goal. Splits the per-goal flow out so the run loop
    can wrap with per-goal exception handling without nesting too
    deep."""
    window = _build_window(tier=tier, goal=goal, now_ms=now_ms)
    run_id = _reflection_run_id(tier=tier, window=window)
    evaluated_at = _evaluated_at_ms(window)

    # Pre-existing evaluation for this (goal, window)? The PK is
    # ``(goal_id, evaluated_at)``; ``INSERT OR IGNORE`` would silently
    # no-op, but checking up-front lets us skip the LLM call entirely
    # — the design's "rerun after a crash never double-counts" wording
    # implies the LLM cost is part of what we save.
    existing = await store.list_evaluations(goal.id)
    if any(ev.evaluated_at_ms == evaluated_at for ev in existing):
        logger.info(
            "reflection_skipped goal_id=%s reason=idempotent run_id=%s",
            goal.id,
            run_id,
        )
        return _PerGoalResult(no_evidence=False, skipped_idempotent=True)

    evidence = await evidence_source.fetch(
        agent_id=agent_id,
        window=window,
        limit=evidence_limit,
    )

    if not evidence:
        # No-evidence sentinel (design §"No episodes fallback"): write
        # ``score=0, narrative='no_evidence', cited=[]`` and skip the
        # LLM call entirely. ``{{goals.failing}}`` excludes these so
        # "no activity" doesn't mask as "actively failing".
        await store.insert_evaluation(
            GoalEvaluation(
                goal_id=goal.id,
                evaluated_at_ms=evaluated_at,
                score_0_to_10=0,
                narrative=NO_EVIDENCE_SENTINEL,
                evidence_episode_ids=[],
                reflection_run_id=run_id,
            )
        )
        return _PerGoalResult(no_evidence=True, skipped_idempotent=False)

    reply = await grader(goal=goal, window=window, evidence=evidence)

    cited = _filter_hallucinated_ids(
        reply.cited_episode_ids,
        allowed=[e.episode_id for e in evidence],
    )
    narrative = _truncate_narrative(reply.narrative)

    await store.insert_evaluation(
        GoalEvaluation(
            goal_id=goal.id,
            evaluated_at_ms=evaluated_at,
            score_0_to_10=int(reply.score_0_to_10),
            narrative=narrative,
            evidence_episode_ids=cited,
            reflection_run_id=run_id,
        )
    )
    return _PerGoalResult(no_evidence=False, skipped_idempotent=False)


__all__ = [
    "NARRATIVE_MAX_CHARS",
    "TIER_TO_WINDOW",
    "Grader",
    "GraderReply",
    "ReflectionSummary",
    "make_callable_grader",
    "make_constant_grader",
    "reflect_once",
]
