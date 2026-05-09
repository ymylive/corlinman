"""Phase 4 W4 D1 — episodic memory.

The package surfaces are layered iter-by-iter — iter 1 shipped the
schema + ``EpisodeKind``; iter 2 added the source-event gatherer and
the run-log CRUD; iter 3 added the importance scorer, the heuristic
classifier, and the LLM distillation pipeline (with PII redaction +
a stub-provider seam for tests); iter 4 wires the end-to-end
:func:`episodes_run_once` runner that ties window selection,
collection, classification, importance, and distillation into one
idempotent pass; iter 5 layers the second-pass embedding writer
(:func:`populate_pending_embeddings`); iter 6 adds the
:mod:`corlinman_episodes.cli` entry point so the production
``corlinman-scheduler`` can spawn the runner via a ``Subprocess``
job and operators can fire one-off catch-up passes from the shell.
Later iters wire the gateway resolver and the on-demand admin route.
"""

from __future__ import annotations

from corlinman_episodes.archive import (
    ARCHIVED_SENTINEL,
    COLD_DIR_NAME,
    COLD_EXEMPT_KINDS,
    ArchiveSummary,
    archive_unreferenced_episodes,
    cold_file_path,
    iter_cold_files,
    render_cold_file,
)
from corlinman_episodes.classifier import classify
from corlinman_episodes.cli import (
    main as cli_main,
)
from corlinman_episodes.cli import (
    register_embedding_provider_factory,
    register_summary_provider_factory,
    run_archive_sweep,
    run_distill_once,
    run_embed_pending,
    run_rehydrate_all,
)
from corlinman_episodes.config import DEFAULT_TENANT_ID, EpisodesConfig
from corlinman_episodes.distiller import (
    PROMPT_SEGMENTS,
    DistilledSummary,
    SummaryFn,
    SummaryProvider,
    distill,
    make_constant_provider,
    make_echo_provider,
    redact_pii,
)
from corlinman_episodes.embed import (
    EmbeddingDimMismatchError,
    EmbeddingFn,
    EmbeddingProvider,
    EmbedSummary,
    decode_embedding,
    encode_embedding,
    populate_pending_embeddings,
)
from corlinman_episodes.importance import score
from corlinman_episodes.rehydrate import (
    ColdEpisode,
    ColdFileMalformedError,
    RehydrateSummary,
    parse_cold_file,
    rehydrate_all,
    rehydrate_episode,
)
from corlinman_episodes.runner import RunSummary, episodes_run_once
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
    PendingEmbeddingRow,
    RunWindowConflict,
    RunWindowConflictError,
    new_episode_id,
    new_run_id,
)

__all__ = [
    "ARCHIVED_SENTINEL",
    "COLD_DIR_NAME",
    "COLD_EXEMPT_KINDS",
    "DEFAULT_TENANT_ID",
    "HOOK_KINDS_OF_INTEREST",
    "PROMPT_SEGMENTS",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_OK",
    "RUN_STATUS_RUNNING",
    "RUN_STATUS_SKIPPED_EMPTY",
    "SCHEMA_SQL",
    "ArchiveSummary",
    "ColdEpisode",
    "ColdFileMalformedError",
    "DistillationRun",
    "DistilledSummary",
    "EmbedSummary",
    "EmbeddingDimMismatchError",
    "EmbeddingFn",
    "EmbeddingProvider",
    "Episode",
    "EpisodeKind",
    "EpisodesConfig",
    "EpisodesStore",
    "HistoryRow",
    "HookEventRow",
    "IdentityMergeRow",
    "PendingEmbeddingRow",
    "RehydrateSummary",
    "RunSummary",
    "RunWindowConflict",
    "RunWindowConflictError",
    "SessionMessage",
    "SignalRow",
    "SourceBundle",
    "SourcePaths",
    "SummaryFn",
    "SummaryProvider",
    "archive_unreferenced_episodes",
    "classify",
    "cli_main",
    "cold_file_path",
    "collect_bundles",
    "decode_embedding",
    "distill",
    "encode_embedding",
    "episodes_run_once",
    "iter_cold_files",
    "make_constant_provider",
    "make_echo_provider",
    "new_episode_id",
    "new_run_id",
    "parse_cold_file",
    "populate_pending_embeddings",
    "redact_pii",
    "register_embedding_provider_factory",
    "register_summary_provider_factory",
    "rehydrate_all",
    "rehydrate_episode",
    "render_cold_file",
    "run_archive_sweep",
    "run_distill_once",
    "run_embed_pending",
    "run_rehydrate_all",
    "score",
    "select_window",
    "window_too_small",
]
