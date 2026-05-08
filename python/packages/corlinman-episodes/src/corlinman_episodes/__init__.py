"""Phase 4 W4 D1 — episodic memory.

Public surface is intentionally narrow at iter 1 — `EpisodeKind`, the
`EpisodesConfig` dataclass, and the SQLite store. Subsequent iters
extend with sources, classifier, importance, distiller, embed, runner.
"""

from __future__ import annotations

from corlinman_episodes.config import DEFAULT_TENANT_ID, EpisodesConfig
from corlinman_episodes.store import (
    SCHEMA_SQL,
    EpisodeKind,
    EpisodesStore,
)

__all__ = [
    "DEFAULT_TENANT_ID",
    "SCHEMA_SQL",
    "EpisodeKind",
    "EpisodesConfig",
    "EpisodesStore",
]
