"""Second-pass embedding writer for episode rows.

Per ``docs/design/phase4-w4-d1-design.md`` §"Distillation job" step 5
and §"Embeddings + retrieval", the runner writes ``Episode`` rows
with ``embedding=NULL`` so a remote-embedding outage does not block
summary persistence. A separate sweep — :func:`populate_pending_embeddings`
— picks up the NULL-vector rows and backfills via the configured
``EmbeddingRouter``-shaped callable.

Why split the pass:
    - Outage isolation. A 503 from the embedding provider should not
      gate the next distillation window — the summary text is the
      operator-readable artefact; embeddings are an optional retrieval
      accelerant.
    - Idempotency. The sweep is naturally idempotent: rows with a
      non-NULL embedding are skipped. Re-running after a partial
      failure picks up exactly where the previous run left off.
    - Dim-mismatch contract. Per the design doc OQ 4, dim mismatch is
      a hard-error at write time (not a silent truncate); operator
      changes ``embedding_provider_alias`` → D1.5 ``reembed`` CLI
      handles re-vectorisation. Iter 5 raises :class:`EmbeddingDimMismatchError`.

The provider is injected as a narrow callable so tests stay offline
— no ``import corlinman_embedding`` dependency. The production wiring
in iter 6+ will hand the runner an
``EmbeddingRouter.embed``-bound method.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from corlinman_episodes.config import DEFAULT_TENANT_ID, EpisodesConfig
from corlinman_episodes.store import EpisodesStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


class EmbeddingProvider(Protocol):
    """Async callable contract for the embedding step.

    Mirrors :meth:`corlinman_embedding.router.EmbeddingRouter.embed`'s
    public shape (a :class:`~collections.abc.Sequence` of strings →
    list-of-list-of-floats). Splitting via ``Protocol`` keeps the
    package free of a hard dependency on ``corlinman-embedding`` —
    the gateway boot wires the real router in production.
    """

    async def __call__(self, texts: Sequence[str]) -> list[list[float]]: ...


#: Convenience alias for non-protocol-aware call sites (``functools.partial``,
#: bound methods, plain ``async def`` lambdas in tests).
EmbeddingFn = Callable[[Sequence[str]], Awaitable[list[list[float]]]]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EmbeddingDimMismatchError(Exception):
    """Raised when a returned embedding's dimension contradicts the
    configured ``embedding_dim``.

    Per the design's OQ 4 ("operator changes embedding_provider_alias;
    old rows have wrong dim"), a dim mismatch is a *hard error* at
    write time. We don't silently truncate or pad — that would corrupt
    the cosine-similarity arithmetic the resolver depends on. Operator
    response: run the (D1.5) ``reembed --since=<ts>`` CLI.
    """

    def __init__(self, *, expected: int, observed: int, episode_id: str) -> None:
        super().__init__(
            f"embedding dim mismatch: expected {expected}, got {observed} "
            f"(episode_id={episode_id!r})"
        )
        self.expected = expected
        self.observed = observed
        self.episode_id = episode_id


# ---------------------------------------------------------------------------
# Result DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbedSummary:
    """Outcome of a single :func:`populate_pending_embeddings` call.

    Mirrors :class:`corlinman_episodes.runner.RunSummary` in style — a
    deterministic record per pass so the operator/admin route can
    surface "wrote N, failed M, skipped K" without parsing logs.
    """

    tenant_id: str
    embedded: int = 0
    failed: int = 0
    failed_episode_ids: tuple[str, ...] = field(default_factory=tuple)
    failed_messages: tuple[str, ...] = field(default_factory=tuple)
    bytes_written: int = 0


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def encode_embedding(vector: Sequence[float]) -> bytes:
    """Pack a float vector as little-endian ``f32`` bytes.

    Schema column is ``BLOB``; the ``f32`` representation is the
    common-denominator format across Python (``struct``), Rust
    (``bytemuck``-flavoured ``f32`` slices), and SQLite (raw bytes).
    Rationale: 32-bit floats keep cosine similarity error well below
    the noise floor of any embedding model we ship, and halve the
    on-disk size relative to ``f64`` — episode rowcount is low but
    the BLOB column is the dominant per-row size.
    """
    return struct.pack(f"<{len(vector)}f", *(float(v) for v in vector))


def decode_embedding(blob: bytes, *, dim: int) -> list[float]:
    """Inverse of :func:`encode_embedding`.

    Validates the byte length matches ``dim * 4``. Used by the
    resolver test layer (iter 7) to round-trip a known vector;
    runtime cosine math runs straight off the bytes via
    ``struct.unpack`` to avoid the list allocation.
    """
    expected = dim * 4
    if len(blob) != expected:
        raise ValueError(
            f"embedding blob size {len(blob)} mismatches dim*4={expected}"
        )
    return list(struct.unpack(f"<{dim}f", blob))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def populate_pending_embeddings(
    *,
    config: EpisodesConfig,
    store: EpisodesStore,
    provider: EmbeddingProvider | EmbeddingFn,
    embedding_dim: int,
    tenant_id: str = DEFAULT_TENANT_ID,
    batch_size: int = 32,
    max_episodes: int | None = None,
) -> EmbedSummary:
    """Walk pending-embedding rows for ``tenant_id`` and populate them.

    Behaviour:
        - Loads up to ``max_episodes`` rows where ``embedding IS NULL``,
          ordered by ``ended_at DESC`` (newest first — operators care
          about recency on a partial-recovery sweep).
        - Calls ``provider`` in batches of ``batch_size``. A batch is
          one provider call carrying all texts concatenated; the
          provider returns one vector per text.
        - On success: per-row UPDATE setting ``embedding`` (BLOB) +
          ``embedding_dim``. The two columns are written in the same
          row update so a partial write can never leave a row with a
          dim but no vector (or vice versa).
        - On dim mismatch: hard-error via :class:`EmbeddingDimMismatchError`.
          The current row stays NULL; rows already written this pass
          remain. Caller decides whether to retry.
        - On any other provider exception: log + record in
          :attr:`EmbedSummary.failed`, leave the row NULL, continue.
          Embedding outage **does not** abort the sweep — partial
          progress is the correct shape (per design §"Embeddings +
          retrieval": a 503 row is retried on the next sweep).

    The function is naturally idempotent: rows that already have a
    non-NULL ``embedding`` are excluded by the SELECT, so re-running
    after a partial failure resumes exactly where it left off.

    ``embedding_dim`` is passed explicitly (rather than read off the
    config) so the caller can quote the dimension the provider was
    configured for — the configured alias may have been swapped out
    between distill and embed passes; failing fast at the type seam
    beats trusting a stale assumption.
    """
    if not config.enabled:
        return EmbedSummary(tenant_id=tenant_id)

    pending = await store.fetch_pending_embeddings(
        tenant_id=tenant_id,
        limit=max_episodes,
    )
    if not pending:
        return EmbedSummary(tenant_id=tenant_id)

    embedded = 0
    failed_ids: list[str] = []
    failed_msgs: list[str] = []
    bytes_written = 0

    # Slice into batches. The provider is free to honour a smaller
    # internal batch — we only care about call-site batching for
    # observability (one log line per provider call).
    for chunk_start in range(0, len(pending), batch_size):
        chunk = pending[chunk_start : chunk_start + batch_size]
        texts = [row.summary_text for row in chunk]
        try:
            vectors = await provider(texts)
        except EmbeddingDimMismatchError:
            # Bubble up — operator must intervene.
            raise
        except Exception as exc:
            # Whole batch failed; stamp every row in the batch as
            # failed and continue. Per-batch granularity is acceptable
            # — a transient 503 typically affects the whole call.
            logger.warning(
                "episodes.embed: batch failed",
                extra={
                    "tenant_id": tenant_id,
                    "chunk_size": len(chunk),
                    "error": str(exc),
                },
            )
            for row in chunk:
                failed_ids.append(row.episode_id)
                failed_msgs.append(str(exc))
            continue

        if len(vectors) != len(chunk):
            # Provider violated its contract; treat it as a bulk failure
            # rather than risking a misaligned write.
            msg = (
                f"provider returned {len(vectors)} vectors for "
                f"{len(chunk)} texts"
            )
            logger.error(
                "episodes.embed: provider arity mismatch",
                extra={"tenant_id": tenant_id, "detail": msg},
            )
            for row in chunk:
                failed_ids.append(row.episode_id)
                failed_msgs.append(msg)
            continue

        for row, vec in zip(chunk, vectors, strict=True):
            if len(vec) != embedding_dim:
                # Hard-error per the design's OQ 4 contract — partial
                # progress so far is preserved (already-committed rows
                # stay), but this pass aborts.
                raise EmbeddingDimMismatchError(
                    expected=embedding_dim,
                    observed=len(vec),
                    episode_id=row.episode_id,
                )
            blob = encode_embedding(vec)
            await store.update_episode_embedding(
                episode_id=row.episode_id,
                embedding=blob,
                embedding_dim=embedding_dim,
            )
            embedded += 1
            bytes_written += len(blob)

    return EmbedSummary(
        tenant_id=tenant_id,
        embedded=embedded,
        failed=len(failed_ids),
        failed_episode_ids=tuple(failed_ids),
        failed_messages=tuple(failed_msgs),
        bytes_written=bytes_written,
    )


__all__ = [
    "EmbedSummary",
    "EmbeddingDimMismatchError",
    "EmbeddingFn",
    "EmbeddingProvider",
    "decode_embedding",
    "encode_embedding",
    "populate_pending_embeddings",
]
