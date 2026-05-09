"""``{{goals.*}}`` placeholder resolver.

Mirrors ``corlinman_persona.placeholders`` so the assembler can register
both behind one engine. Four canonical keys; anything else under
``goals.<custom>`` resolves to ``""`` (typo-tolerant, same posture as the
persona resolver).

Resolution rules (per design ``§"{{goals.*}} placeholder semantics"``):

- ``{{goals.today}}`` — bare bullets of short-tier, active, future-
  dated goals ordered by ``created_at``. No scores (the day isn't graded
  yet). Empty string if none.
- ``{{goals.weekly}}`` — mid-tier active goals **plus** the previous
  week's ``goal_evaluations`` summary lines for that tier
  (``- <body>: score 7 — <one-line narrative>``). Bounded to 8 lines
  total; trailing ``… (+N more)`` if truncated (open-question §4 in the
  design — adopted here).
- ``{{goals.quarterly}}`` — long-tier active goals + the last 12 weekly
  mid-tier scores rolled up (``avg``, ``min``, ``count_failing``).
- ``{{goals.failing}}`` — active goals whose **most recent**
  ``score_0_to_10 < 5``, regardless of tier. Bounded to 5; the
  ``no_evidence`` sentinel narrative is excluded ("no activity" ≠
  "actively failing").

Unknown ``agent_id`` always resolves to ``""`` — prompt rendering is a
hot path and noisy errors there are worse than empty placeholders.

The resolver does not cache. Callers wanting per-render memoisation
should wrap us; we keep this layer obviously consistent with the DB.
"""

from __future__ import annotations

import time
from typing import Final

from corlinman_goals.state import Goal, GoalEvaluation
from corlinman_goals.store import DEFAULT_TENANT_ID, GoalStore

# Caps for the bounded placeholder outputs. Surfaced as constants so the
# assembler / tests can import them without re-deriving the design's
# numbers.
WEEKLY_LINE_CAP: Final[int] = 8
FAILING_LINE_CAP: Final[int] = 5
QUARTERLY_TRAILING_WEEKS: Final[int] = 12

# Sentinel narrative the reflection job writes when no episodes were
# available. ``{{goals.failing}}`` excludes these because "no activity"
# is not the same as "actively failing".
NO_EVIDENCE_SENTINEL: Final[str] = "no_evidence"

_MS_PER_SECOND: Final[int] = 1000
_SECONDS_PER_DAY: Final[int] = 86_400
_WEEK_MS: Final[int] = 7 * _SECONDS_PER_DAY * _MS_PER_SECOND


def _now_ms() -> int:
    """Unix milliseconds; pulled out so tests can monkeypatch if needed.

    The persona resolver doesn't currently parameterise time; we adopt
    the same shape (one private helper) so tests bypass it via
    ``monkeypatch.setattr`` rather than threading a clock through the
    resolver constructor.
    """
    return int(time.time() * 1000)


def _bullet(body: str) -> str:
    """Format one goal body as a bullet line.

    Single space after ``-`` matches the assembler's expectations and
    the existing ``corlinman_persona.recent_topics`` formatting style
    (comma-joined, no leading bullets) — different products, different
    formats; both stable contracts.
    """
    return f"- {body}"


def _scored_bullet(body: str, score: int, narrative: str) -> str:
    """Bullet with the design's ``- <body>: score 7 — <narrative>`` form.

    Narrative may contain CJK punctuation or quotes — pass through as-is
    rather than re-escape; the prompt is the consumer.
    """
    return f"- {body}: score {score} — {narrative}"


def _truncated_suffix(remaining: int) -> str:
    """Trailing line emitted when the cap clips the output.

    Format: ``- … (+3 more)`` so the prompt sees a recognisable bullet,
    not a bare line.
    """
    return f"- … (+{remaining} more)"


class GoalsResolver:
    """Read-only resolver for ``{{goals.*}}`` placeholder keys.

    Holds a reference to a :class:`GoalStore` and answers one lookup at
    a time. Tenant scope defaults to ``"default"`` for the same reason
    the store does — single-tenant deployments don't have to thread
    ``tenant_id`` through every prompt render.
    """

    def __init__(
        self,
        store: GoalStore,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> None:
        self._store = store
        self._tenant_id = tenant_id

    async def resolve(self, key: str, agent_id: str) -> str:
        """Return the placeholder value for ``key`` against ``agent_id``.

        ``key`` is the suffix after ``goals.`` — one of ``"today"``,
        ``"weekly"``, ``"quarterly"``, ``"failing"``, or any unknown
        string (which always returns ``""``). Per-key implementations
        live in dedicated methods so tests can drive each one without
        going through ``resolve``.
        """
        if not agent_id:
            # Same defensive posture as ``corlinman_persona.placeholders``:
            # missing agent_id is a no-data scenario, not an error.
            return ""
        if key == "today":
            return await self._resolve_today(agent_id)
        if key == "weekly":
            return await self._resolve_weekly(agent_id)
        if key == "quarterly":
            return await self._resolve_quarterly(agent_id)
        if key == "failing":
            return await self._resolve_failing(agent_id)
        # Typo-tolerant fallback — see module docstring rationale.
        return ""

    # ------------------------------------------------------------------
    # today — bare bullets, no scores (the day isn't graded yet)
    # ------------------------------------------------------------------

    async def _resolve_today(self, agent_id: str) -> str:
        active_short = await self._store.list_goals(
            agent_id=agent_id,
            tier="short",
            status="active",
            tenant_id=self._tenant_id,
        )
        now = _now_ms()
        future_dated = [g for g in active_short if g.target_date_ms >= now]
        if not future_dated:
            return ""
        return "\n".join(_bullet(g.body) for g in future_dated)

    # ------------------------------------------------------------------
    # weekly — mid-tier bodies + previous-week scored bullets, capped at 8
    # ------------------------------------------------------------------

    async def _resolve_weekly(self, agent_id: str) -> str:
        active_mid = await self._store.list_goals(
            agent_id=agent_id,
            tier="mid",
            status="active",
            tenant_id=self._tenant_id,
        )
        if not active_mid:
            return ""
        now = _now_ms()
        prev_week_lo = now - 2 * _WEEK_MS
        prev_week_hi = now - _WEEK_MS

        # Walk the active mid goals, look up the most recent evaluation
        # falling inside the previous-week window, and emit a scored
        # bullet if one exists; bare body otherwise.
        lines: list[str] = []
        for goal in active_mid:
            scored = await self._most_recent_eval_in_range(
                goal, lo_ms=prev_week_lo, hi_ms=prev_week_hi
            )
            if scored is None:
                lines.append(_bullet(goal.body))
            else:
                lines.append(
                    _scored_bullet(goal.body, scored.score_0_to_10, scored.narrative)
                )

        return self._cap_lines(lines)

    # ------------------------------------------------------------------
    # quarterly — long-tier bodies + trailing 12-week mid-tier roll-up
    # ------------------------------------------------------------------

    async def _resolve_quarterly(self, agent_id: str) -> str:
        active_long = await self._store.list_goals(
            agent_id=agent_id,
            tier="long",
            status="active",
            tenant_id=self._tenant_id,
        )
        if not active_long:
            # Long-tier roll-up has no anchor without a long-tier goal;
            # empty placeholder beats a header-only output.
            return ""
        lines = [_bullet(g.body) for g in active_long]

        # Trailing-12-week mid-tier roll-up. Walk every active mid goal
        # for this agent, gather their evaluations within the window,
        # and aggregate.
        mid_goals = await self._store.list_goals(
            agent_id=agent_id,
            tier="mid",
            status="active",
            tenant_id=self._tenant_id,
        )
        now = _now_ms()
        window_start = now - QUARTERLY_TRAILING_WEEKS * _WEEK_MS
        scores: list[int] = []
        for g in mid_goals:
            evaluations = await self._store.list_evaluations(g.id)
            for ev in evaluations:
                if ev.evaluated_at_ms < window_start or ev.evaluated_at_ms > now:
                    continue
                if ev.narrative == NO_EVIDENCE_SENTINEL:
                    # Sentinel rows are no-activity, not signal — exclude
                    # them from avg/min the same way ``failing`` does.
                    continue
                scores.append(ev.score_0_to_10)
        if scores:
            avg = sum(scores) / len(scores)
            lines.append(
                "- mid-tier last "
                f"{QUARTERLY_TRAILING_WEEKS}w: "
                f"avg {avg:.1f}, min {min(scores)}, "
                f"count_failing {sum(1 for s in scores if s < 5)}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # failing — active goals whose latest score < 5 (any tier), capped at 5
    # ------------------------------------------------------------------

    async def _resolve_failing(self, agent_id: str) -> str:
        active = await self._store.list_goals(
            agent_id=agent_id,
            status="active",
            tenant_id=self._tenant_id,
        )
        failing: list[tuple[Goal, GoalEvaluation]] = []
        for goal in active:
            latest = await self._store.list_evaluations(goal.id, limit=1)
            if not latest:
                continue
            ev = latest[0]
            if ev.narrative == NO_EVIDENCE_SENTINEL:
                # ``no_evidence`` is "we ran reflection but there were no
                # episodes to grade against" — explicitly *not* failing.
                continue
            if ev.score_0_to_10 < 5:
                failing.append((goal, ev))
        if not failing:
            return ""

        lines = [
            _scored_bullet(goal.body, ev.score_0_to_10, ev.narrative)
            for goal, ev in failing
        ]
        return self._cap_lines(lines, cap=FAILING_LINE_CAP)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def _most_recent_eval_in_range(
        self,
        goal: Goal,
        *,
        lo_ms: int,
        hi_ms: int,
    ) -> GoalEvaluation | None:
        """Return the most recent evaluation for ``goal`` whose
        ``evaluated_at_ms`` falls inside ``[lo_ms, hi_ms]``.

        Half-closed (``hi`` inclusive) so a Sunday-evening reflection
        run shows up in the following week's "last week" lookup. The
        store list is most-recent-first, so we can return the first
        match without sorting.
        """
        evaluations = await self._store.list_evaluations(goal.id)
        for ev in evaluations:
            if lo_ms <= ev.evaluated_at_ms <= hi_ms:
                if ev.narrative == NO_EVIDENCE_SENTINEL:
                    # Same exclusion rule as ``failing`` — no signal,
                    # no surface in the prompt.
                    continue
                return ev
        return None

    def _cap_lines(self, lines: list[str], *, cap: int = WEEKLY_LINE_CAP) -> str:
        """Apply the ``cap`` and append the truncation sentinel if needed.

        Implements the resolution adopted from open-question §4: emit
        ``- … (+N more)`` so a downstream agent reading the prompt knows
        truncation happened, instead of silently believing N is the
        whole list.
        """
        if len(lines) <= cap:
            return "\n".join(lines)
        # Reserve one line for the suffix so total still fits ``cap``.
        kept = lines[: cap - 1]
        remaining = len(lines) - len(kept)
        kept.append(_truncated_suffix(remaining))
        return "\n".join(kept)


__all__ = [
    "FAILING_LINE_CAP",
    "NO_EVIDENCE_SENTINEL",
    "QUARTERLY_TRAILING_WEEKS",
    "WEEKLY_LINE_CAP",
    "GoalsResolver",
]
