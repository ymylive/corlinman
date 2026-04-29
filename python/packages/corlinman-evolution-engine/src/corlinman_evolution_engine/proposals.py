"""Proposal dataclass, id minting, and the ``KindHandler`` strategy.

Mirrors the Rust ``EvolutionProposal`` shape (see
``rust/crates/corlinman-evolution/src/types.rs``) but only the fields a
freshly-generated proposal needs. Status, decision and apply timestamps are
left to the gateway / approval flow and start as ``pending`` / ``NULL``.

The ``KindHandler`` protocol is the Phase 3 hook. Phase 2 only ships
``MemoryOpHandler``, but the engine dispatches through this interface so
adding a Phase 3 ``SkillExtractionHandler`` (the closed learning loop
pattern from `nous-research/hermes-agent` — task → skill extraction →
refinement) is purely additive: register a new handler class and add the
corresponding kind to ``EngineConfig.enabled_kinds``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from corlinman_evolution_engine.store import DEFAULT_TENANT_ID

if TYPE_CHECKING:
    from corlinman_evolution_engine.clustering import SignalCluster

# ---------------------------------------------------------------------------
# Proposal dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvolutionProposal:
    """A proposal a handler intends to persist.

    The engine itself owns id minting + database insertion, so handlers
    return these in-memory and never touch SQLite directly. ``id`` is filled
    in by the engine right before the INSERT — handlers leave it empty.

    ``tenant_id`` propagates the tenant of the originating signal cluster
    onto the proposal so the Rust applier (and the operator UI) can route
    the proposal back to the right tenant queue. Single-tenant deployments
    leave it at ``"default"``.
    """

    kind: str
    target: str
    diff: str
    reasoning: str
    risk: str
    budget_cost: int
    signal_ids: list[int]
    trace_ids: list[str]
    id: str = ""
    tenant_id: str = DEFAULT_TENANT_ID

    def with_id(self, proposal_id: str) -> EvolutionProposal:
        """Return a copy with ``id`` set. Used by the engine at persist time."""
        return EvolutionProposal(
            kind=self.kind,
            target=self.target,
            diff=self.diff,
            reasoning=self.reasoning,
            risk=self.risk,
            budget_cost=self.budget_cost,
            signal_ids=list(self.signal_ids),
            trace_ids=list(self.trace_ids),
            id=proposal_id,
            tenant_id=self.tenant_id,
        )


def format_day_prefix(now_ms: int) -> str:
    """Format the ``evol-YYYY-MM-DD`` prefix used in proposal ids."""
    dt = datetime.fromtimestamp(now_ms / 1000.0, tz=UTC)
    return f"evol-{dt.strftime('%Y-%m-%d')}"


def mint_proposal_id(day_prefix: str, sequence_number: int) -> str:
    """Combine ``day_prefix`` with a 3-digit sequence number.

    ``sequence_number`` is 1-based. ``mint_proposal_id("evol-2026-04-25", 1)``
    yields ``evol-2026-04-25-001``.
    """
    if sequence_number < 1 or sequence_number > 999:
        raise ValueError(
            f"sequence_number must be between 1 and 999, got {sequence_number}"
        )
    return f"{day_prefix}-{sequence_number:03d}"


# ---------------------------------------------------------------------------
# Handler context + protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposalContext:
    """Read-only state the engine hands every handler.

    Includes the clusters that triggered this run plus the kb path so a
    handler can open its own connections (e.g. ``MemoryOpHandler`` reads
    ``kb.sqlite``; a Phase 3 ``SkillExtractionHandler`` would read recent
    session transcripts from elsewhere).
    """

    clusters: list[SignalCluster]
    kb_path: Path
    similarity_threshold: float
    max_chunks_scanned: int
    now_ms: int


class KindHandler(Protocol):
    """Strategy contract for a single ``EvolutionKind``.

    Implementations are pure data → data: given a context they emit
    candidate proposals. The engine owns dedup, id minting, the
    ``max_proposals_per_run`` cap and the SQL INSERT — handlers should not
    touch the evolution database directly.

    Phase 3 candidates (each one a separate handler):

    - ``SkillExtractionHandler`` — turns successful task transcripts into
      reusable skill cards (hermes-agent's closed learning loop).
    - ``TagRebalanceHandler`` — proposes tag-tree merges based on
      tagmemo's pyramid features.
    - ``RetryTuningHandler`` — bumps tool retry counts on persistent
      transient failures.

    Each one slots in next to ``MemoryOpHandler`` without touching the
    engine's main loop.
    """

    @property
    def kind(self) -> str:
        """The ``EvolutionKind`` wire string this handler emits (e.g. ``"memory_op"``)."""
        ...

    async def existing_targets(self, conn: object) -> set[tuple[str, str]]:
        """Return ``(target, tenant_id)`` pairs already proposed for this kind.

        Receives the engine's ``aiosqlite`` connection (typed as ``object``
        here so the protocol stays small and import-free); concrete
        handlers can cast inside.

        Phase 4 W1 4-1D: the dedup key is ``(target, tenant_id)`` rather
        than bare ``target`` so two tenants with the same target string
        (e.g. both editing ``agent.greeting``) get independent proposals.
        Pre-4-1A schemas that lack the ``tenant_id`` column materialise
        every row with ``tenant_id="default"`` — single-tenant
        deployments behave identically to Phase 2 / 3.
        """
        ...

    async def propose(self, ctx: ProposalContext) -> list[EvolutionProposal]:
        """Emit candidate proposals for the current run.

        Order matters: the engine respects insertion order when applying
        ``max_proposals_per_run``, so handlers should sort their output
        strongest-signal-first.
        """
        ...
