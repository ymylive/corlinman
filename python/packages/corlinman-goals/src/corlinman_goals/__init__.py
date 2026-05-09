"""Goal hierarchies — short/mid/long agent goals + reflection scoring.

See :class:`GoalStore` for the SQLite-backed store and :class:`Goal` /
:class:`GoalEvaluation` for the row dataclasses. The design is at
``docs/design/phase4-w4-d2-design.md``; the placeholder resolver, CLI,
and reflection job land in later iters of this package.
"""

from __future__ import annotations

from corlinman_goals.evaluator import (
    LONG_TRAILING_MID_WEEKS,
    LongScore,
    MidScore,
    aggregate_long,
    aggregate_mid,
)
from corlinman_goals.evidence import (
    DEFAULT_EVIDENCE_LIMIT,
    EpisodeEvidence,
    EpisodesStoreEvidence,
    EvidenceEpisode,
    StaticEvidence,
)
from corlinman_goals.placeholders import (
    FAILING_LINE_CAP,
    NO_EVIDENCE_SENTINEL,
    QUARTERLY_TRAILING_WEEKS,
    WEEKLY_LINE_CAP,
    GoalsResolver,
)
from corlinman_goals.reflection import (
    NARRATIVE_MAX_CHARS,
    Grader,
    GraderReply,
    ReflectionSummary,
    make_callable_grader,
    make_constant_grader,
    reflect_once,
)
from corlinman_goals.state import (
    SOURCE_VALUES,
    STATUS_VALUES,
    TIER_VALUES,
    Goal,
    GoalEvaluation,
)
from corlinman_goals.store import DEFAULT_TENANT_ID, SCHEMA_SQL, GoalStore
from corlinman_goals.windows import (
    LONG_WINDOW_DAYS,
    MID_WINDOW_DAYS,
    SHORT_WINDOW_HOURS,
    Window,
    default_target_date_ms,
    long_window,
    mid_window,
    short_window,
    tier_rank,
)

__all__ = [
    "DEFAULT_EVIDENCE_LIMIT",
    "DEFAULT_TENANT_ID",
    "FAILING_LINE_CAP",
    "LONG_TRAILING_MID_WEEKS",
    "LONG_WINDOW_DAYS",
    "MID_WINDOW_DAYS",
    "NARRATIVE_MAX_CHARS",
    "NO_EVIDENCE_SENTINEL",
    "QUARTERLY_TRAILING_WEEKS",
    "SCHEMA_SQL",
    "SHORT_WINDOW_HOURS",
    "SOURCE_VALUES",
    "STATUS_VALUES",
    "TIER_VALUES",
    "WEEKLY_LINE_CAP",
    "EpisodeEvidence",
    "EpisodesStoreEvidence",
    "EvidenceEpisode",
    "Goal",
    "GoalEvaluation",
    "GoalStore",
    "GoalsResolver",
    "Grader",
    "GraderReply",
    "LongScore",
    "MidScore",
    "ReflectionSummary",
    "StaticEvidence",
    "Window",
    "aggregate_long",
    "aggregate_mid",
    "default_target_date_ms",
    "long_window",
    "make_callable_grader",
    "make_constant_grader",
    "mid_window",
    "reflect_once",
    "short_window",
    "tier_rank",
]
