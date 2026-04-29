"""Phase 3 W3-A — chunk consolidation job.

Periodically (default: 04:00 UTC daily) the scheduler runs
``corlinman-evolution-engine consolidate-once`` which calls into
:func:`consolidation_run_once`. The job:

1. Opens ``kb.sqlite`` read-only.
2. Picks chunks whose stored ``decay_score`` is at or above
   ``promotion_threshold`` AND whose ``namespace`` is not yet
   ``consolidated``. Sorted decay-score desc so the strongest signals
   land first when ``max_promotions_per_run`` truncates the candidate
   list.
3. For each candidate, writes one ``memory_op`` proposal targeted at
   ``consolidate_chunk:<id>`` to ``evolution.sqlite``. The actual
   namespace flip happens when the gateway's ``EvolutionApplier``
   processes the proposal — we deliberately do **not** mutate
   ``kb.sqlite`` here. Reason: every kb mutation must flow through the
   single approve/apply/audit pipeline so revert + monitoring stay
   coherent.

The handler reuses the Phase 2 dedup contract (skip rows whose target
already lives in ``evolution_proposals``) so a second run on the same
candidate set is a no-op.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from corlinman_evolution_engine.memory_op import KIND_MEMORY_OP

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config + summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsolidationConfig:
    """Tunables for one consolidation pass.

    Mirrors ``[memory.consolidation]`` in the workspace TOML; populated
    from the ``MemoryConsolidationConfig`` Rust struct via the
    ``--config`` flag on the CLI. ``enabled=false`` short-circuits the
    run with a clear log line so flipping the master switch off doesn't
    require touching the scheduler entry.

    Phase 3.1 (B-4): ``cooling_period_hours`` defends against the
    cold-start cliff. After the W3-A migration every legacy row sits
    at ``decay_score = 1.0`` (the column default), so a naive
    "decay_score >= threshold" filter would promote the first 50
    chunks the SELECT returns on the very first cron tick. The
    cooling period requires a chunk to have been recalled at least
    once AND for that recall to be older than the cooling window
    before it qualifies — burst-read material gets time to settle.
    """

    enabled: bool = True
    promotion_threshold: float = 0.65
    max_promotions_per_run: int = 50
    cooling_period_hours: float = 24.0


@dataclass
class ConsolidationSummary:
    """Outcome of one ``consolidation_run_once`` invocation."""

    candidates_found: int = 0
    proposals_written: int = 0
    skipped_existing: int = 0
    skipped_disabled: bool = False
    elapsed_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------


async def consolidation_run_once(
    *,
    config: ConsolidationConfig,
    kb_db_path: Path,
    evolution_db_path: Path,
) -> ConsolidationSummary:
    """Single consolidation pass — see module docstring.

    Both DB paths are required; we never fall back to defaults so a
    misconfigured scheduler entry surfaces loudly. The summary is
    returned to the CLI so operators can grep stdout for skip/written
    counts.
    """
    summary = ConsolidationSummary()
    started_at = time.monotonic()

    if not config.enabled:
        summary.skipped_disabled = True
        summary.elapsed_seconds = time.monotonic() - started_at
        logger.info("consolidation: master switch disabled; nothing to do")
        return summary

    now_ms = int(time.time() * 1_000)
    candidates = await _list_promotion_candidates(
        kb_db_path,
        threshold=config.promotion_threshold,
        limit=config.max_promotions_per_run,
        cooling_period_hours=config.cooling_period_hours,
        now_ms=now_ms,
    )
    summary.candidates_found = len(candidates)
    if not candidates:
        summary.elapsed_seconds = time.monotonic() - started_at
        logger.info(
            "consolidation: no candidates above threshold=%.2f",
            config.promotion_threshold,
        )
        return summary

    day_prefix = _format_day_prefix(now_ms)

    async with aiosqlite.connect(evolution_db_path) as conn:
        await conn.execute("PRAGMA foreign_keys = ON")
        existing = await _existing_consolidate_targets(conn)

        # Mint ids continuing from whatever already exists for the day
        # (matching the EvolutionEngine convention so two CLIs running
        # back-to-back don't collide).
        seq_offset = await _count_proposals_on_day(conn, day_prefix)
        written = 0
        for chunk_id, decay_score in candidates:
            target = f"consolidate_chunk:{chunk_id}"
            if target in existing:
                summary.skipped_existing += 1
                continue
            proposal_id = _mint_proposal_id(day_prefix, seq_offset + written + 1)
            reasoning = _reasoning_for(chunk_id, decay_score)
            await conn.execute(
                """INSERT INTO evolution_proposals
                     (id, kind, target, diff, reasoning, risk, budget_cost, status,
                      shadow_metrics, signal_ids, trace_ids,
                      created_at, decided_at, decided_by, applied_at, rollback_of)
                   VALUES (?, ?, ?, '', ?, 'low', 0, 'pending',
                           NULL, '[]', '[]',
                           ?, NULL, NULL, NULL, NULL)""",
                (proposal_id, KIND_MEMORY_OP, target, reasoning, now_ms),
            )
            existing.add(target)
            written += 1
        await conn.commit()
        summary.proposals_written = written

    summary.elapsed_seconds = time.monotonic() - started_at
    logger.info(
        "consolidation: candidates=%d written=%d skipped_existing=%d elapsed=%.2fs",
        summary.candidates_found,
        summary.proposals_written,
        summary.skipped_existing,
        summary.elapsed_seconds,
    )
    return summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


CONSOLIDATED_NAMESPACE = "consolidated"
"""Mirror of the Rust ``corlinman_vector::CONSOLIDATED_NAMESPACE``
constant. Kept inline (not imported) so this module doesn't need to
shell out to Rust at test time.
"""


async def _list_promotion_candidates(
    kb_path: Path,
    *,
    threshold: float,
    limit: int,
    cooling_period_hours: float,
    now_ms: int,
) -> list[tuple[int, float]]:
    """Query the kb for rows whose decay_score >= threshold and whose
    namespace is not yet ``consolidated``. Ordered by decay_score desc.

    Returns ``[(chunk_id, decay_score), ...]``. Uses ``mode=ro`` because
    the consolidation job never writes to kb.sqlite — the
    EvolutionApplier owns that mutation.

    Phase 3.1 (B-4): a chunk must have been recalled at least once
    AND the recall must be older than ``cooling_period_hours`` to
    qualify. Mirrors the Rust-side
    ``SqliteStore::list_promotion_candidates`` guard so both consumer
    paths behave identically (the Rust path is what tests exercise
    directly; this Python path is what the scheduler hits).
    """
    if limit <= 0:
        return []
    cooling_ms = (
        int(cooling_period_hours * 3_600_000)
        if cooling_period_hours and cooling_period_hours > 0
        else 0
    )
    cutoff_ms = max(0, now_ms - cooling_ms)
    uri = f"file:{kb_path}?mode=ro"
    async with aiosqlite.connect(uri, uri=True) as conn:
        cursor = await conn.execute(
            """SELECT id, decay_score FROM chunks
               WHERE decay_score >= ?
                 AND namespace != ?
                 AND last_recalled_at IS NOT NULL
                 AND last_recalled_at <= ?
               ORDER BY decay_score DESC, id ASC
               LIMIT ?""",
            (float(threshold), CONSOLIDATED_NAMESPACE, int(cutoff_ms), int(limit)),
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return [(int(r[0]), float(r[1])) for r in rows]


# Statuses that should keep blocking a re-proposal of the same chunk.
# `denied` and `rolled_back` are intentionally excluded so an operator
# decision can be revisited once the chunk's signal recovers — Phase 3
# W3-A's status-agnostic dedup permanently silenced any chunk an
# operator had ever rejected, which makes "类人 forgetting" a one-way
# door.
_DEDUP_BLOCKING_STATUSES: tuple[str, ...] = ("pending", "approved", "applied")


async def _existing_consolidate_targets(conn: aiosqlite.Connection) -> set[str]:
    """Pull every in-flight ``memory_op`` target that's a
    ``consolidate_chunk:`` — used for dedup so a second run doesn't
    double-file proposals on the same chunks.

    Phase 3.1 (B-2): only ``pending`` / ``approved`` / ``applied``
    proposals block re-filing. A ``denied`` or ``rolled_back`` target
    becomes eligible again on the next run, so an operator's reject
    isn't a permanent ban — the chunk has to re-clear the score +
    cooling guards before it gets back into the candidate set anyway.
    """
    placeholders = ",".join("?" for _ in _DEDUP_BLOCKING_STATUSES)
    sql = (
        "SELECT target FROM evolution_proposals "
        "WHERE kind = ? AND target LIKE 'consolidate_chunk:%' "
        f"AND status IN ({placeholders})"
    )
    cursor = await conn.execute(sql, (KIND_MEMORY_OP, *_DEDUP_BLOCKING_STATUSES))
    rows = await cursor.fetchall()
    await cursor.close()
    return {str(r[0]) for r in rows}


async def _count_proposals_on_day(
    conn: aiosqlite.Connection, day_prefix: str
) -> int:
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM evolution_proposals WHERE id LIKE ?",
        (f"{day_prefix}%",),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return int(row[0]) if row is not None else 0


def _format_day_prefix(now_ms: int) -> str:
    """``evol-YYYY-MM-DD`` prefix used in proposal ids. Mirrors
    :func:`corlinman_evolution_engine.proposals.format_day_prefix` —
    duplicated here to keep the consolidation module standalone.
    """
    dt = datetime.fromtimestamp(now_ms / 1000.0, tz=UTC)
    return f"evol-{dt.strftime('%Y-%m-%d')}"


def _mint_proposal_id(day_prefix: str, sequence_number: int) -> str:
    if sequence_number < 1 or sequence_number > 999:
        raise ValueError(
            f"sequence_number must be between 1 and 999, got {sequence_number}"
        )
    return f"{day_prefix}-{sequence_number:03d}"


def _reasoning_for(chunk_id: int, decay_score: float) -> str:
    """Human-readable ``reasoning`` field for a consolidation proposal."""
    return (
        f"decay_score={decay_score:.2f} sustained on chunk {chunk_id}; "
        f"promote to consolidated namespace"
    )


@contextmanager
def _close_loop_logger() -> Iterator[None]:  # pragma: no cover - belt + braces
    """Reserved for future logging-cleanup hooks. Currently a no-op."""
    yield
