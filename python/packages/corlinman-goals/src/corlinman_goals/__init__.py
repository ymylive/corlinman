"""Goal hierarchies — short/mid/long agent goals + reflection scoring.

See :class:`GoalStore` for the SQLite-backed store and :class:`Goal` /
:class:`GoalEvaluation` for the row dataclasses. The design is at
``docs/design/phase4-w4-d2-design.md``; the placeholder resolver, CLI,
and reflection job land in later iters of this package.
"""

from __future__ import annotations

from corlinman_goals.state import (
    SOURCE_VALUES,
    STATUS_VALUES,
    TIER_VALUES,
    Goal,
    GoalEvaluation,
)
from corlinman_goals.store import DEFAULT_TENANT_ID, SCHEMA_SQL, GoalStore

__all__ = [
    "DEFAULT_TENANT_ID",
    "SCHEMA_SQL",
    "SOURCE_VALUES",
    "STATUS_VALUES",
    "TIER_VALUES",
    "Goal",
    "GoalEvaluation",
    "GoalStore",
]
