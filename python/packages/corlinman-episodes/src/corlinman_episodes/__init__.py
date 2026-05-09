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

from corlinman_episodes.classifier import classify
from corlinman_episodes.cli import (
    main as cli_main,
)
from corlinman_episodes.cli import (
    register_embedding_provider_factory,
    register_summary_provider_factory,
    run_distill_once,
    run_embed_pending,
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
    "DEFAULT_TENANT_ID",
    "HOOK_KINDS_OF_INTEREST",
    "PROMPT_SEGMENTS",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_OK",
    "RUN_STATUS_RUNNING",
    "RUN_STATUS_SKIPPED_EMPTY",
    "SCHEMA_SQL",
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
    "RunSummary",
    "RunWindowConflict",
    "RunWindowConflictError",
    "SessionMessage",
    "SignalRow",
    "SourceBundle",
    "SourcePaths",
    "SummaryFn",
    "SummaryProvider",
    "classify",
    "cli_main",
    "collect_bundles",
    "decode_embedding",
    "distill",
    "encode_embedding",
    "episodes_run_once",
    "make_constant_provider",
    "make_echo_provider",
    "new_episode_id",
    "new_run_id",
    "populate_pending_embeddings",
    "redact_pii",
    "register_embedding_provider_factory",
    "register_summary_provider_factory",
    "run_distill_once",
    "run_embed_pending",
    "score",
    "select_window",
    "window_too_small",
]
