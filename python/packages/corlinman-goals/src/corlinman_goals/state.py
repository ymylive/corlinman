"""Goal / GoalEvaluation dataclasses — the in-memory projection of one
``goals`` or ``goal_evaluations`` row.

Mutations on these objects do nothing; the source of truth is
:class:`~corlinman_goals.store.GoalStore`. The dataclasses are intentionally
lightweight and carry no I/O — they exist so callers can return typed
results without leaking ``aiosqlite.Row`` shapes through their API.

Schema reference: ``docs/design/phase4-w4-d2-design.md`` §"Schema".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

# CHECK-clause-allowed values, exposed as constants so the resolver and CLI
# can validate inputs without re-reading the DDL. Sets, not tuples, because
# the only operation we ever do is membership testing.
TIER_VALUES: Final[frozenset[str]] = frozenset({"short", "mid", "long"})
STATUS_VALUES: Final[frozenset[str]] = frozenset(
    {"active", "completed", "expired", "archived"}
)
SOURCE_VALUES: Final[frozenset[str]] = frozenset(
    {"operator_cli", "operator_ui", "agent_self", "seed"}
)

# Sentinel narrative the reflection job writes when no episodes were
# available. ``{{goals.failing}}`` excludes these because "no activity"
# is not the same as "actively failing". Lives here (not in
# :mod:`placeholders`) so :mod:`evaluator` and :mod:`reflection` can
# import it without dragging in the placeholder ↔ evaluator cycle that
# iter 8's cascade-aware ``{{goals.weekly}}`` would otherwise create.
NO_EVIDENCE_SENTINEL: Final[str] = "no_evidence"


@dataclass
class Goal:
    """Single ``goals`` row.

    Attributes
    ----------
    id
        Stable goal id, conventionally ``"goal-<yyyymmdd>-<slug>"``.
    agent_id
        Agent that owns the goal. Goals never travel across agents within
        one tenant.
    tier
        One of ``"short"`` / ``"mid"`` / ``"long"``. The reflection cron
        and window math both key off this value.
    body
        One-sentence goal statement. The reflection LLM grades episodes
        against this string verbatim.
    created_at_ms
        Unix milliseconds — when the goal was authored.
    target_date_ms
        Unix milliseconds — when the goal "matures" (used by
        ``{{goals.today}}`` to filter the active set; see design §"Tier
        windows").
    parent_goal_id
        Optional reference to a parent goal id; long ← mid ← short. Cross-
        tier order is enforced by the CLI, not the schema.
    status
        One of :data:`STATUS_VALUES`. ``"active"`` is the only state the
        placeholder reads.
    source
        Provenance — see design §"Goal sources" for the authority matrix.
    """

    id: str
    agent_id: str
    tier: str
    body: str
    created_at_ms: int
    target_date_ms: int
    parent_goal_id: str | None = None
    status: str = "active"
    source: str = "operator_cli"


@dataclass
class GoalEvaluation:
    """Single ``goal_evaluations`` row — the audit trail of a reflection run.

    Each row is keyed on ``(goal_id, evaluated_at_ms)``. ``reflection_run_id``
    provides idempotency: re-running the same window must not write a new
    row, achieved by ``INSERT OR IGNORE`` against the primary key.

    ``evidence_episode_ids`` is the JSON-decoded list of episode ids the
    grader cited; the store rejects ids the grader hallucinated (i.e. ids
    that were not in the evidence pool the LLM was shown).
    """

    goal_id: str
    evaluated_at_ms: int
    score_0_to_10: int
    narrative: str
    evidence_episode_ids: list[str] = field(default_factory=list)
    reflection_run_id: str = ""


__all__ = [
    "NO_EVIDENCE_SENTINEL",
    "SOURCE_VALUES",
    "STATUS_VALUES",
    "TIER_VALUES",
    "Goal",
    "GoalEvaluation",
]
