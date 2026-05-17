"""corlinman EvolutionLoop persistence layer (Python port).

Python sibling of the Rust crate ``corlinman-evolution``. Owns the same
three concerns:

1. **Types** — :class:`EvolutionSignal`, :class:`EvolutionProposal`,
   :class:`EvolutionHistory` plus the :class:`EvolutionKind` /
   :class:`EvolutionRisk` / :class:`EvolutionStatus` enums.
2. **Schema** — the :data:`SCHEMA_SQL` constant. A fresh
   :meth:`EvolutionStore.open` applies it idempotently
   (``CREATE … IF NOT EXISTS``).
3. **Repos** — async SQLite repos for signals / proposals / history /
   apply-intent log.

The schema is the cross-language contract — Rust observers (admin API,
gateway hooks) and the Python EvolutionEngine both bind to the same
``evolution.sqlite`` file via the column names defined in
:mod:`corlinman_evolution_store.schema`.
"""

from __future__ import annotations

from corlinman_evolution_store.repo import (
    ApplyIntent,
    EvolutionGuardConfig,
    HistoryRepo,
    IntentLogRepo,
    MalformedEnumError,
    MalformedJsonError,
    NotFoundError,
    ProposalsRepo,
    RecursionGuardCooldownError,
    RecursionGuardViolationError,
    RepoError,
    SignalsRepo,
    SqliteRepoError,
    iso_week_window,
)
from corlinman_evolution_store.schema import (
    MIGRATIONS,
    POST_MIGRATIONS_SQL,
    SCHEMA_SQL,
)
from corlinman_evolution_store.store import EvolutionStore, OpenError
from corlinman_evolution_store.types import (
    DEFAULT_TENANT_ID,
    ClusterThresholdPayload,
    EngineConfigPayload,
    EnginePromptPayload,
    EvolutionHistory,
    EvolutionKind,
    EvolutionProposal,
    EvolutionRisk,
    EvolutionSignal,
    EvolutionStatus,
    ObserverFilterPayload,
    ParseError,
    ProposalId,
    ShadowMetrics,
    SignalSeverity,
)

__all__ = [
    "DEFAULT_TENANT_ID",
    "MIGRATIONS",
    "POST_MIGRATIONS_SQL",
    "SCHEMA_SQL",
    "ApplyIntent",
    "ClusterThresholdPayload",
    "EngineConfigPayload",
    "EnginePromptPayload",
    "EvolutionGuardConfig",
    "EvolutionHistory",
    "EvolutionKind",
    "EvolutionProposal",
    "EvolutionRisk",
    "EvolutionSignal",
    "EvolutionStatus",
    "EvolutionStore",
    "HistoryRepo",
    "IntentLogRepo",
    "MalformedEnumError",
    "MalformedJsonError",
    "NotFoundError",
    "ObserverFilterPayload",
    "OpenError",
    "ParseError",
    "ProposalId",
    "ProposalsRepo",
    "RecursionGuardCooldownError",
    "RecursionGuardViolationError",
    "RepoError",
    "ShadowMetrics",
    "SignalSeverity",
    "SignalsRepo",
    "SqliteRepoError",
    "iso_week_window",
]
