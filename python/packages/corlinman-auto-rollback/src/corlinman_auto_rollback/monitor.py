"""``AutoRollbackMonitor`` — orchestrates one pass over applied
proposals still inside the grace window.

Ported 1:1 from ``rust/crates/corlinman-auto-rollback/src/monitor.rs``.

Per :meth:`AutoRollbackMonitor.run_once`:

1. Pull ``applied`` proposals from ``[now - grace_window, now]`` via
   :meth:`ProposalsRepo.list_applied_in_grace_window`.
2. For each row:

   - Resolve the per-kind metric whitelist via
     :func:`~corlinman_auto_rollback.metrics.watched_event_kinds`.
     Kinds without one are skipped (we never auto-revert a kind we
     don't yet have a signal contract for; counted as "skipped" not
     "inspected").
   - Load the apply-time baseline via
     :meth:`HistoryRepo.latest_for_proposal` and parse its
     ``metrics_baseline`` JSON into a :class:`MetricSnapshot`.
   - Take a fresh post-apply snapshot via
     :func:`~corlinman_auto_rollback.metrics.capture_snapshot`.
   - Run :func:`~corlinman_auto_rollback.metrics.compute_delta` +
     :func:`~corlinman_auto_rollback.metrics.breaches_threshold`.
   - On breach, call :meth:`Applier.revert` and update the per-row
     counters. Per-row failures degrade into ``summary.errors`` /
     ``summary.rollbacks_failed`` rather than aborting the run.
3. Return a :class:`RunSummary` for the CLI / scheduler log line.

The monitor never consults the ``enabled`` flag itself — the CLI
short-circuits at the entry point so ``run_once`` is always a
"do the work" call.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from corlinman_evolution_store import (
    EvolutionStore,
    HistoryRepo,
    NotFoundError,
    ProposalsRepo,
)

from corlinman_auto_rollback.config import (
    AutoRollbackThresholds,
    EvolutionAutoRollbackConfig,
)
from corlinman_auto_rollback.metrics import (
    MetricSnapshot,
    breaches_threshold,
    capture_snapshot,
    compute_delta,
    watched_event_kinds,
)
from corlinman_auto_rollback.revert import (
    Applier,
    NotAppliedRevertError,
    RevertError,
)

logger = logging.getLogger(__name__)


def now_ms() -> int:
    """Wall-clock unix milliseconds. Pulled out so tests can monkey-patch
    if they need to pin a deterministic clock."""
    return int(time.time() * 1000)


@dataclass
class RunSummary:
    """Counts surfaced by :meth:`AutoRollbackMonitor.run_once` so the
    CLI / scheduler can log a one-line summary (and future Prometheus
    counters can read off this shape directly)."""

    proposals_inspected: int = 0
    thresholds_breached: int = 0
    rollbacks_triggered: int = 0
    rollbacks_succeeded: int = 0
    rollbacks_failed: int = 0
    errors: int = 0


# Mirrors the Rust default (50, tracking the engine's per-run cap).
DEFAULT_MAX_PROPOSALS_PER_RUN = 50


class AutoRollbackMonitor:
    """Orchestrate one auto-rollback pass.

    Takes injected repos + applier + an opened :class:`EvolutionStore`
    pool so the monitor owns one source of truth for signal counts.
    Construction does not consult ``config.enabled`` — wire that gate
    at the CLI / scheduler boundary.
    """

    def __init__(
        self,
        proposals: ProposalsRepo,
        history: HistoryRepo,
        evolution_store: EvolutionStore,
        applier: Applier,
        config: EvolutionAutoRollbackConfig,
    ) -> None:
        self._proposals = proposals
        self._history = history
        self._evolution_store = evolution_store
        self._applier = applier
        self._grace_window_hours = config.grace_window_hours
        self._thresholds: AutoRollbackThresholds = config.thresholds
        self._max_proposals_per_run = DEFAULT_MAX_PROPOSALS_PER_RUN

    def with_max_proposals_per_run(self, n: int) -> AutoRollbackMonitor:
        """Operator override — primarily for tests + one-off backfills.
        The default (50) tracks the engine's per-run proposal cap.
        Returns ``self`` for chaining (Rust ``mut self -> Self``)."""
        self._max_proposals_per_run = n
        return self

    async def run_once(self) -> RunSummary:
        """One pass over applied-in-grace-window proposals."""
        summary = RunSummary()
        wall_now_ms = now_ms()

        try:
            candidates = await self._proposals.list_applied_in_grace_window(
                wall_now_ms,
                self._grace_window_hours,
                self._max_proposals_per_run,
            )
        except Exception as exc:
            logger.warning(
                "auto_rollback: list_applied_in_grace_window failed: %s", exc
            )
            summary.errors += 1
            return summary

        for proposal in candidates:
            # Per-kind whitelist — kinds without one are intentionally
            # not rolled back rather than declared "fine"; skip silently.
            watched = watched_event_kinds(proposal.kind)
            if not watched:
                logger.debug(
                    "auto_rollback: no whitelist for kind %s; skipping (proposal_id=%s)",
                    proposal.kind.as_str(),
                    proposal.id,
                )
                continue

            # Apply-time baseline. Missing here is data corruption: the
            # forward applier wrote the row before flipping status.
            try:
                history = await self._history.latest_for_proposal(proposal.id)
            except NotFoundError:
                logger.warning(
                    "auto_rollback: history missing for applied proposal — corruption (proposal_id=%s)",
                    proposal.id,
                )
                summary.errors += 1
                continue
            except Exception as exc:
                logger.warning(
                    "auto_rollback: history fetch failed (proposal_id=%s): %s",
                    proposal.id,
                    exc,
                )
                summary.errors += 1
                continue

            # Parse baseline JSON. A malformed baseline must not auto-
            # revert — fail safe and let the operator inspect.
            try:
                baseline: MetricSnapshot = MetricSnapshot.from_dict(
                    history.metrics_baseline
                )
            except ValueError as exc:
                logger.warning(
                    "auto_rollback: malformed metrics_baseline JSON; skipping (proposal_id=%s): %s",
                    proposal.id,
                    exc,
                )
                summary.errors += 1
                continue

            try:
                current = await capture_snapshot(
                    self._evolution_store.conn,
                    proposal.target,
                    watched,
                    self._thresholds.signal_window_secs,
                    wall_now_ms,
                )
            except Exception as exc:
                logger.warning(
                    "auto_rollback: capture_snapshot failed (proposal_id=%s): %s",
                    proposal.id,
                    exc,
                )
                summary.errors += 1
                continue

            delta = compute_delta(baseline, current)
            summary.proposals_inspected += 1

            reason = breaches_threshold(delta, self._thresholds)
            if reason is None:
                continue

            summary.thresholds_breached += 1
            summary.rollbacks_triggered += 1

            try:
                await self._applier.revert(proposal.id, reason)
            except NotAppliedRevertError as exc:
                # Race with operator / a concurrent monitor pass —
                # benign, log + count as failed-but-not-error.
                logger.info(
                    "auto_rollback: revert raced — already not applied (proposal_id=%s, status=%s)",
                    proposal.id,
                    exc.status,
                )
                summary.rollbacks_failed += 1
            except RevertError as exc:
                logger.warning(
                    "auto_rollback: revert failed (proposal_id=%s): %s",
                    proposal.id,
                    exc,
                )
                summary.rollbacks_failed += 1
            except Exception as exc:
                # Defensive: an applier raising something outside the
                # typed hierarchy still counts as a failed rollback so
                # the run line stays truthful.
                logger.warning(
                    "auto_rollback: revert raised unexpected exception (proposal_id=%s): %s",
                    proposal.id,
                    exc,
                )
                summary.rollbacks_failed += 1
            else:
                summary.rollbacks_succeeded += 1
                logger.info(
                    "auto_rollback: revert succeeded (proposal_id=%s, kind=%s, reason=%s)",
                    proposal.id,
                    proposal.kind.as_str(),
                    reason,
                )

        return summary


__all__ = [
    "DEFAULT_MAX_PROPOSALS_PER_RUN",
    "AutoRollbackMonitor",
    "RunSummary",
    "now_ms",
]
