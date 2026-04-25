"""``EvolutionEngine`` — the Phase 2 daily run loop.

A single ``run_once()`` pass does:

1. Load signals observed in the last ``lookback_days``.
2. Cluster by ``(event_kind, target)``.
3. If at least one actionable cluster exists, dispatch to every enabled
   ``KindHandler`` (Phase 2 default: just ``MemoryOpHandler``). Each
   handler returns a list of candidate ``EvolutionProposal`` objects.
4. The engine dedups against already-filed targets, mints sequential daily
   ids, and persists each proposal with ``status=pending``.

No LLM calls. No shadow testing. No automatic application — that's the
gateway's ``EvolutionApplier`` after the operator (or auto-approval rule)
acts on the queue.

Strategy hook for Phase 3: ``EngineConfig.enabled_kinds`` decides which
handlers run. Adding a new handler is purely additive — register the
handler class in ``DEFAULT_HANDLERS`` (or pass via ``handlers=`` to the
engine) and add the kind string to ``enabled_kinds``. See the
``KindHandler`` protocol in ``proposals.py`` for the contract; the closed
learning loop pattern from ``nous-research/hermes-agent`` (task →
extracted skill → refinement) maps directly to a future
``SkillExtractionHandler``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from corlinman_evolution_engine.clustering import SignalCluster, cluster_signals
from corlinman_evolution_engine.memory_op import KIND_MEMORY_OP, MemoryOpHandler
from corlinman_evolution_engine.proposals import (
    EvolutionProposal,
    KindHandler,
    ProposalContext,
    format_day_prefix,
    mint_proposal_id,
)
from corlinman_evolution_engine.store import EvolutionStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

#: The handler registry the engine consults when ``handlers=None`` is passed.
#: Phase 3 adds entries here (or callers pass an explicit list); the engine
#: itself stays untouched. Keep the keys in sync with the wire strings used
#: in ``EngineConfig.enabled_kinds`` and the Rust ``EvolutionKind`` enum.
DEFAULT_HANDLERS: dict[str, KindHandler] = {
    KIND_MEMORY_OP: MemoryOpHandler(),
}


@dataclass(frozen=True)
class EngineConfig:
    """Knobs for one engine run."""

    db_path: Path = Path("/data/evolution.sqlite")
    kb_path: Path = Path("/data/kb.sqlite")
    lookback_days: int = 7
    min_cluster_size: int = 3
    max_proposals_per_run: int = 10
    run_budget_seconds: int = 60
    similarity_threshold: float = 0.95
    max_chunks_scanned: int = 5_000
    """Hard cap on chunks pulled out of ``kb.sqlite`` per run.

    Scan is O(n^2) so this matters. 5k chunks → ~12.5M Jaccard comparisons,
    which finishes well inside the 60s budget for short text. Bump only if
    you've measured.
    """

    enabled_kinds: tuple[str, ...] = (KIND_MEMORY_OP,)
    """Which ``KindHandler`` registrations to run, in order.

    Phase 2 default: ``("memory_op",)``. Phase 3 will extend with e.g.
    ``("memory_op", "skill_update", "tag_rebalance")`` once the
    corresponding handlers land.
    """


@dataclass
class RunSummary:
    """Outcome of one ``run_once()``. Returned to the CLI / scheduler."""

    signals_loaded: int = 0
    clusters_found: int = 0
    duplicate_pairs_found: int = 0
    proposals_written: int = 0
    skipped_existing: int = 0
    truncated_by_cap: bool = False
    skipped_by_budget: bool = False
    elapsed_seconds: float = 0.0
    cluster_summaries: list[str] = field(default_factory=list)
    proposals_by_kind: dict[str, int] = field(default_factory=dict)


def _now_ms() -> int:
    return int(time.time() * 1_000)


def _resolve_handlers(
    enabled_kinds: tuple[str, ...],
    overrides: Sequence[KindHandler] | None,
) -> list[KindHandler]:
    """Map ``enabled_kinds`` to handler instances.

    ``overrides`` lets tests inject fakes without touching the global
    registry. Unknown kinds raise — silently dropping them would let a
    typo in ``enabled_kinds`` produce zero proposals with no signal.
    """
    if overrides is not None:
        registry: dict[str, KindHandler] = {h.kind: h for h in overrides}
    else:
        registry = DEFAULT_HANDLERS

    resolved: list[KindHandler] = []
    for kind in enabled_kinds:
        handler = registry.get(kind)
        if handler is None:
            raise ValueError(
                f"no KindHandler registered for kind={kind!r}; "
                f"known kinds: {sorted(registry.keys())}"
            )
        resolved.append(handler)
    return resolved


class EvolutionEngine:
    """Stateless engine over an evolution + kb pair of SQLite files.

    Pass ``handlers=`` to override ``DEFAULT_HANDLERS`` (used by tests; in
    production the registry is the source of truth). Only handlers whose
    ``kind`` is in ``config.enabled_kinds`` actually run.
    """

    def __init__(
        self,
        config: EngineConfig,
        *,
        handlers: Sequence[KindHandler] | None = None,
    ) -> None:
        self.config = config
        self._handlers = _resolve_handlers(config.enabled_kinds, handlers)

    async def run_once(self) -> RunSummary:
        """One full pass: signals → clusters → handlers → persist."""
        cfg = self.config
        started_at = time.monotonic()
        summary = RunSummary()
        deadline = started_at + cfg.run_budget_seconds

        async with EvolutionStore(cfg.db_path) as evolution:
            # 1. Load signals.
            now_ms = _now_ms()
            since_ms = now_ms - cfg.lookback_days * 86_400_000
            signals = await evolution.list_signals_since(since_ms)
            summary.signals_loaded = len(signals)

            # 2. Cluster.
            clusters = cluster_signals(signals, min_cluster_size=cfg.min_cluster_size)
            summary.clusters_found = len(clusters)
            summary.cluster_summaries = [_describe_cluster(c) for c in clusters]
            if not clusters:
                summary.elapsed_seconds = time.monotonic() - started_at
                logger.info(
                    "evolution: no clusters above threshold; nothing to do "
                    "(signals=%d, lookback_days=%d)",
                    summary.signals_loaded,
                    cfg.lookback_days,
                )
                return summary

            # Budget check — clustering should be cheap, but bail before
            # invoking handlers if we've already burned the budget on a
            # huge signal load.
            if time.monotonic() >= deadline:
                summary.skipped_by_budget = True
                summary.elapsed_seconds = time.monotonic() - started_at
                logger.warning(
                    "evolution: budget exhausted before handler dispatch "
                    "(clusters=%d)",
                    len(clusters),
                )
                return summary

            ctx = ProposalContext(
                clusters=clusters,
                kb_path=cfg.kb_path,
                similarity_threshold=cfg.similarity_threshold,
                max_chunks_scanned=cfg.max_chunks_scanned,
                now_ms=now_ms,
            )

            # 3. Run each enabled handler. Each handler is responsible for
            # ordering its candidates by signal strength.
            day_prefix = format_day_prefix(now_ms)
            seq_offset = await evolution.count_proposals_on_day(day_prefix)
            written = 0
            stop = False

            for handler in self._handlers:
                if stop:
                    break
                if time.monotonic() >= deadline:
                    summary.skipped_by_budget = True
                    logger.warning(
                        "evolution: hit run_budget_seconds=%d before %s "
                        "could run",
                        cfg.run_budget_seconds,
                        handler.kind,
                    )
                    break

                candidates = await handler.propose(ctx)
                if handler.kind == KIND_MEMORY_OP:
                    # Surfaced as a top-level field for backwards-compat
                    # with Phase 2 callers / tests.
                    summary.duplicate_pairs_found = len(candidates)
                if not candidates:
                    continue

                existing = await handler.existing_targets(evolution.conn)
                kind_written = 0
                for proposal in candidates:
                    if time.monotonic() >= deadline:
                        summary.skipped_by_budget = True
                        stop = True
                        logger.warning(
                            "evolution: hit run_budget_seconds=%d after "
                            "%d proposals; remaining deferred",
                            cfg.run_budget_seconds,
                            written,
                        )
                        break
                    if written >= cfg.max_proposals_per_run:
                        summary.truncated_by_cap = True
                        stop = True
                        logger.info(
                            "evolution: hit max_proposals_per_run=%d; "
                            "remaining deferred",
                            cfg.max_proposals_per_run,
                        )
                        break
                    if proposal.target in existing:
                        summary.skipped_existing += 1
                        continue

                    proposal_id = mint_proposal_id(
                        day_prefix, seq_offset + written + 1
                    )
                    await _persist(
                        evolution=evolution,
                        proposal=proposal.with_id(proposal_id),
                        created_at=now_ms,
                    )
                    existing.add(proposal.target)
                    written += 1
                    kind_written += 1

                if kind_written:
                    summary.proposals_by_kind[handler.kind] = kind_written

            summary.proposals_written = written

        summary.elapsed_seconds = time.monotonic() - started_at
        logger.info(
            "evolution: run complete; signals=%d clusters=%d written=%d "
            "skipped_existing=%d elapsed=%.2fs by_kind=%s",
            summary.signals_loaded,
            summary.clusters_found,
            summary.proposals_written,
            summary.skipped_existing,
            summary.elapsed_seconds,
            dict(summary.proposals_by_kind),
        )
        return summary


def _describe_cluster(cluster: SignalCluster) -> str:
    target = cluster.target if cluster.target is not None else "<no-target>"
    return f"{cluster.event_kind}:{target} (n={cluster.size})"


async def _persist(
    *,
    evolution: EvolutionStore,
    proposal: EvolutionProposal,
    created_at: int,
) -> None:
    """Write one ``EvolutionProposal`` to ``evolution_proposals``."""
    await evolution.insert_proposal(
        proposal_id=proposal.id,
        kind=proposal.kind,
        target=proposal.target,
        diff=proposal.diff,
        reasoning=proposal.reasoning,
        risk=proposal.risk,
        budget_cost=proposal.budget_cost,
        signal_ids=proposal.signal_ids,
        trace_ids=proposal.trace_ids,
        created_at=created_at,
    )
