"""Generate ``tag_rebalance`` proposals from recall-drop signal clusters.

Phase 3-2B Step 1 ships exactly one tag_rebalance flavour: ``merge_tag``. The
detector watches ``evolution_signals`` for ``tag.recall.dropped`` events
(observer-emitted when a query mentions a tag but no chunk under it ranks).
A cluster of these on the same tag path is the canonical structural-drift
signal: either the tag's children scattered into siblings, or the tag itself
collapsed into a single-child branch.

Per the ``KindHandler`` contract, this handler is pure data → data: it does
NOT read ``tag_nodes`` or any other external state. The proposal target is
*symbolic* in v0.3 — encoded as ``merge_tag:<path>`` — and the Step 2
``EvolutionApplier`` does the kb lookup to resolve the path to real node ids
plus the chosen sibling. Keeping kb access in the applier keeps this layer
swappable and avoids dragging vector-store coupling into the engine.

``collapse_tag:<id>`` is reserved for a follow-up; the same handler protocol
covers it without engine changes once the applier learns to handle it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from corlinman_evolution_engine.clustering import SignalCluster
from corlinman_evolution_engine.proposals import EvolutionProposal, ProposalContext
from corlinman_evolution_engine.store import fetch_existing_targets

if TYPE_CHECKING:
    import aiosqlite

KIND_TAG_REBALANCE = "tag_rebalance"

# Trigger event_kind. The observer emits this whenever a search resolves a
# tag in the query but every chunk under that tag ranks below the recall
# floor. A repeat of the same tag path is the structural-drift fingerprint.
EVENT_TAG_RECALL_DROPPED = "tag.recall.dropped"


def _merge_target(path: str) -> str:
    """Symbolic target for a path-level merge proposal.

    Format: ``merge_tag:<path>``. The applier resolves <path> to a concrete
    ``(src_id, dst_id)`` pair when it runs. Stable string so dedup keys line
    up across runs.
    """
    return f"merge_tag:{path}"


def _reasoning_for(cluster: SignalCluster) -> str:
    """Human-readable ``reasoning`` field for a tag-rebalance proposal."""
    return (
        f"tag recall dropped: {cluster.size} signals on path "
        f"{cluster.target!r} indicate the tag's children no longer match "
        f"queries that mention it"
    )


class TagRebalanceHandler:
    """``KindHandler`` for the ``tag_rebalance`` kind.

    Phase 3-2B Step 1 implementation: scan the engine's already-clustered
    signals for ``tag.recall.dropped`` clusters and emit one symbolic
    ``merge_tag:<path>`` proposal per cluster. The applier (Step 2)
    resolves the path to concrete ``tag_nodes`` ids.

    ``existing_targets`` reads ``evolution_proposals`` to dedup against
    targets already filed (any status), mirroring ``MemoryOpHandler``.
    """

    @property
    def kind(self) -> str:
        return KIND_TAG_REBALANCE

    async def existing_targets(self, conn: object) -> set[tuple[str, str]]:
        # Same cast pattern as MemoryOpHandler — KindHandler keeps aiosqlite
        # out of the protocol surface.
        sqlite_conn: aiosqlite.Connection = conn  # type: ignore[assignment]
        return await fetch_existing_targets(sqlite_conn, self.kind)

    async def propose(self, ctx: ProposalContext) -> list[EvolutionProposal]:
        # Engine already gated clusters by ``min_cluster_size``; we only
        # filter by event_kind. ``target`` is the tag path string.
        relevant = [
            c
            for c in ctx.clusters
            if c.event_kind == EVENT_TAG_RECALL_DROPPED and c.target
        ]
        if not relevant:
            return []

        # Strongest cluster first — the engine respects insertion order
        # when it applies ``max_proposals_per_run``.
        relevant.sort(key=lambda c: c.size, reverse=True)

        return [
            EvolutionProposal(
                kind=self.kind,
                target=_merge_target(cluster.target or ""),
                diff="",  # tag_rebalance encodes the op in target, not diff.
                reasoning=_reasoning_for(cluster),
                risk="medium",
                budget_cost=1,
                signal_ids=cluster.signal_ids,
                trace_ids=cluster.trace_ids,
                tenant_id=cluster.tenant_id,
            )
            for cluster in relevant
        ]
