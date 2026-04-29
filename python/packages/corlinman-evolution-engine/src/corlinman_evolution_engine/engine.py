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

import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from corlinman_evolution_engine.clustering import SignalCluster, cluster_signals
from corlinman_evolution_engine.memory_op import KIND_MEMORY_OP, MemoryOpHandler
from corlinman_evolution_engine.prompt_template import (
    KIND_PROMPT_TEMPLATE,
    PromptTemplateHandler,
)
from corlinman_evolution_engine.proposals import (
    EvolutionProposal,
    KindHandler,
    ProposalContext,
    format_day_prefix,
    mint_proposal_id,
)
from corlinman_evolution_engine.skill_update import (
    KIND_SKILL_UPDATE,
    SkillUpdateHandler,
)
from corlinman_evolution_engine.store import EvolutionStore
from corlinman_evolution_engine.tag_rebalance import (
    KIND_TAG_REBALANCE,
    TagRebalanceHandler,
)
from corlinman_evolution_engine.tool_policy import (
    KIND_TOOL_POLICY,
    ToolPolicyHandler,
)

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
    KIND_TAG_REBALANCE: TagRebalanceHandler(),
    KIND_SKILL_UPDATE: SkillUpdateHandler(),
    # Phase 4 W1 4-1D: high-risk kinds gated by the docker shadow
    # sandbox (4-1C). The engine still emits the proposals; the
    # ShadowTester decides whether they reach the operator queue.
    KIND_PROMPT_TEMPLATE: PromptTemplateHandler(),
    KIND_TOOL_POLICY: ToolPolicyHandler(),
}


@dataclass(frozen=True)
class BudgetConfig:
    """Per-week / per-kind cap on how many proposals a run may file.

    Mirrors ``[evolution.budget]`` in the workspace TOML; populated by
    ``cli.py`` from the ``--budget-config`` path. ``enabled=false`` reverts
    to the Phase 2 / 3 W1-A behavior (no cap beyond ``max_proposals_per_run``).

    A ``per_kind`` entry is the *only* extra gate for that kind on top of
    ``weekly_total``. A kind missing from ``per_kind`` is bounded only by
    ``weekly_total``.
    """

    enabled: bool = False
    weekly_total: int = 15
    per_kind: dict[str, int] = field(default_factory=dict)


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

    enabled_kinds: tuple[str, ...] = (
        KIND_MEMORY_OP,
        KIND_TAG_REBALANCE,
        KIND_SKILL_UPDATE,
        KIND_PROMPT_TEMPLATE,
        KIND_TOOL_POLICY,
    )
    """Which ``KindHandler`` registrations to run, in order.

    Phase 4 W1 4-1D default: the three Phase-3 handlers plus the two
    new high-risk Phase-4 handlers. All three Phase-3 kinds are
    medium / low risk; the two new Phase-4 kinds are always
    ``risk="high"`` so the W1-A ShadowTester routes them through the
    docker sandbox (W1-C) before they reach the operator queue. The
    engine itself just emits the proposals; the safety net does the
    gating.
    """

    budget: BudgetConfig = field(default_factory=BudgetConfig)
    """Phase 3 W1-C: per-week / per-kind proposal cap. Default: disabled."""


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
    """Phase 2 / 3-W1-A flag: the ``run_budget_seconds`` wall clock expired."""
    elapsed_seconds: float = 0.0
    cluster_summaries: list[str] = field(default_factory=list)
    proposals_by_kind: dict[str, int] = field(default_factory=dict)
    proposals_skipped_budget: int = 0
    """Phase 3 W1-C counter: proposals dropped by the per-week / per-kind cap."""
    budget_skips_by_kind: dict[str, int] = field(default_factory=dict)


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
                    # Phase 4 W1 4-1D: dedup key is ``(target, tenant_id)``
                    # so two tenants with the same target string each
                    # land their own proposal. Single-tenant deployments
                    # see no behaviour change (every row carries
                    # ``tenant_id="default"``).
                    dedup_key = (proposal.target, proposal.tenant_id)
                    if dedup_key in existing:
                        summary.skipped_existing += 1
                        continue

                    skip_reason = await _check_budget(
                        evolution=evolution,
                        kind=proposal.kind,
                        budget=cfg.budget,
                        now_ms=now_ms,
                    )
                    if skip_reason is not None:
                        summary.proposals_skipped_budget += 1
                        summary.budget_skips_by_kind[proposal.kind] = (
                            summary.budget_skips_by_kind.get(proposal.kind, 0) + 1
                        )
                        logger.warning(
                            "evolution: budget skip kind=%s target=%s reason=%s",
                            proposal.kind,
                            proposal.target,
                            skip_reason,
                        )
                        continue

                    proposal_id = mint_proposal_id(
                        day_prefix, seq_offset + written + 1
                    )
                    await _persist(
                        evolution=evolution,
                        proposal=proposal.with_id(proposal_id),
                        created_at=now_ms,
                    )
                    existing.add(dedup_key)
                    written += 1
                    kind_written += 1

                if kind_written:
                    summary.proposals_by_kind[handler.kind] = kind_written

            summary.proposals_written = written

            if summary.proposals_skipped_budget > 0:
                await _emit_budget_signal(
                    evolution=evolution,
                    summary=summary,
                    now_ms=now_ms,
                )

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
    """Write one ``EvolutionProposal`` to ``evolution_proposals``.

    ``tenant_id`` flows from the proposal (which got it from the
    originating signal cluster) into the row. Single-tenant deployments
    keep using ``"default"`` and the store skips the column on schemas
    that haven't yet adopted Phase 4 W1 4-1A.
    """
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
        tenant_id=proposal.tenant_id,
    )


# ---------------------------------------------------------------------------
# Phase 3 W1-C: budget enforcement
# ---------------------------------------------------------------------------


def _iso_week_start_ms(now_ms: int) -> int:
    """Monday 00:00:00 UTC of the ISO week containing ``now_ms``.

    Mirrors the Rust ``ProposalsRepo::count_proposals_in_iso_week`` boundary
    so both languages count the same window. ``datetime.weekday()`` returns
    0 for Monday.
    """
    dt = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
    monday = dt - timedelta(days=dt.weekday())
    monday_start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(monday_start.timestamp() * 1000)


async def _count_proposals_since(
    evolution: EvolutionStore,
    since_ms: int,
    *,
    kind: str | None,
) -> int:
    """Count rows in ``evolution_proposals`` with ``created_at >= since_ms``.

    ``kind=None`` counts across all kinds (used for ``weekly_total``);
    a non-None ``kind`` filters to one (used for ``per_kind`` caps). Same
    SQL shape as the Rust ``ProposalsRepo::count_proposals_in_iso_week`` so
    both languages read the same window.
    """
    if kind is None:
        cursor = await evolution.conn.execute(
            "SELECT COUNT(*) FROM evolution_proposals WHERE created_at >= ?",
            (since_ms,),
        )
    else:
        cursor = await evolution.conn.execute(
            "SELECT COUNT(*) FROM evolution_proposals "
            "WHERE created_at >= ? AND kind = ?",
            (since_ms, kind),
        )
    row = await cursor.fetchone()
    await cursor.close()
    return int(row[0]) if row is not None else 0


async def _check_budget(
    *,
    evolution: EvolutionStore,
    kind: str,
    budget: BudgetConfig,
    now_ms: int,
) -> str | None:
    """Return ``None`` for green light, or a human-readable reason to skip.

    Counts are recomputed per call so multiple skips in a single batch see
    the new totals — once we hit a cap, every subsequent candidate of that
    kind is skipped without an extra query past the count.
    """
    if not budget.enabled:
        return None
    week_start = _iso_week_start_ms(now_ms)
    total_used = await _count_proposals_since(evolution, week_start, kind=None)
    if total_used >= budget.weekly_total:
        return f"weekly_total {budget.weekly_total} reached"
    cap = budget.per_kind.get(kind)
    if cap is not None:
        kind_used = await _count_proposals_since(evolution, week_start, kind=kind)
        if kind_used >= cap:
            return f"per-kind {kind} cap {cap} reached"
    return None


async def _emit_budget_signal(
    *,
    evolution: EvolutionStore,
    summary: RunSummary,
    now_ms: int,
) -> None:
    """Insert one ``evolution.budget.exceeded`` signal summarising the run.

    Emitted once per run (not per skip) to avoid signal spam. ``target`` is
    the kind with the most skips this run; ties resolve alphabetically so
    the signal is deterministic across replays.
    """
    skips = summary.budget_skips_by_kind
    # Sort by (-count, kind) so highest-count wins; alphabetical breaks ties.
    target_kind = min(skips.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    payload = {
        "weekly_total_used": summary.proposals_skipped_budget,
        "per_kind_skips": dict(skips),
    }
    await evolution.conn.execute(
        """INSERT INTO evolution_signals
             (event_kind, target, severity, payload_json,
              trace_id, session_id, observed_at)
           VALUES ('evolution.budget.exceeded', ?, 'warn', ?, NULL, NULL, ?)""",
        (target_kind, json.dumps(payload), now_ms),
    )
    await evolution.conn.commit()
