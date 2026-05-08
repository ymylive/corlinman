"""Phase 4 W4 D1 — episodic memory.

The package surfaces are layered iter-by-iter — iter 1 shipped the
schema + ``EpisodeKind``; iter 2 adds episode/run dataclasses, the
``insert_episode`` + run-log CRUD, and the multi-stream
``collect_bundles`` source-event gatherer. Subsequent iters extend
with classifier, importance, distiller, embed, runner.
"""

from __future__ import annotations

from corlinman_episodes.config import DEFAULT_TENANT_ID, EpisodesConfig
from corlinman_episodes.sources import (
    HOOK_KINDS_OF_INTEREST,
    HistoryRow,
    HookEventRow,
    IdentityMergeRow,
    SessionMessage,
    SignalRow,
    SourceBundle,
    SourcePaths,
    collect_bundles,
    select_window,
    window_too_small,
)
from corlinman_episodes.store import (
    RUN_STATUS_FAILED,
    RUN_STATUS_OK,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SKIPPED_EMPTY,
    SCHEMA_SQL,
    DistillationRun,
    Episode,
    EpisodeKind,
    EpisodesStore,
    RunWindowConflict,
    RunWindowConflictError,
    new_episode_id,
    new_run_id,
)

__all__ = [
    "DEFAULT_TENANT_ID",
    "HOOK_KINDS_OF_INTEREST",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_OK",
    "RUN_STATUS_RUNNING",
    "RUN_STATUS_SKIPPED_EMPTY",
    "SCHEMA_SQL",
    "DistillationRun",
    "Episode",
    "EpisodeKind",
    "EpisodesConfig",
    "EpisodesStore",
    "HistoryRow",
    "HookEventRow",
    "IdentityMergeRow",
    "RunWindowConflict",
    "RunWindowConflictError",
    "SessionMessage",
    "SignalRow",
    "SourceBundle",
    "SourcePaths",
    "collect_bundles",
    "new_episode_id",
    "new_run_id",
    "select_window",
    "window_too_small",
]
