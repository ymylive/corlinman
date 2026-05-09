"""Goal hierarchies — short/mid/long agent goals + reflection scoring.

See :class:`GoalStore` for the SQLite-backed store and :class:`Goal` /
:class:`GoalEvaluation` for the row dataclasses. The design is at
``docs/design/phase4-w4-d2-design.md``; the placeholder resolver, CLI,
and reflection job land in later iters of this package.
"""

from __future__ import annotations

from corlinman_goals.placeholders import (
    FAILING_LINE_CAP,
    NO_EVIDENCE_SENTINEL,
    QUARTERLY_TRAILING_WEEKS,
    WEEKLY_LINE_CAP,
    GoalsResolver,
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
    "DEFAULT_TENANT_ID",
    "FAILING_LINE_CAP",
    "LONG_WINDOW_DAYS",
    "MID_WINDOW_DAYS",
    "NO_EVIDENCE_SENTINEL",
    "QUARTERLY_TRAILING_WEEKS",
    "SCHEMA_SQL",
    "SHORT_WINDOW_HOURS",
    "SOURCE_VALUES",
    "STATUS_VALUES",
    "TIER_VALUES",
    "WEEKLY_LINE_CAP",
    "Goal",
    "GoalEvaluation",
    "GoalStore",
    "GoalsResolver",
    "Window",
    "default_target_date_ms",
    "long_window",
    "mid_window",
    "short_window",
    "tier_rank",
]
