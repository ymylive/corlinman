"""Generate ``agent_card`` proposals from agent-identity signal clusters.

Phase 4 W1 4-1D follow-up: when the observer detects that the live agent
persona has drifted from the character card (e.g. tone shifts away from
the documented voice, on-topic guardrails ignored), it emits
``agent.identity_drift`` or ``agent.persona_misalignment`` events whose
``target`` is the agent name (e.g. ``casual``, ``researcher``,
``default``). A cluster of those past the threshold becomes an
``agent_card`` proposal asking the operator to rewrite that agent's
character card.

These proposals are reserved for ``risk = "high"`` because applying one
literally rewrites the agent's identity. The Phase 3 W1-A ShadowTester
already routes high-risk kinds through the shadow stage, and the Phase 4
W1 4-1C docker sandbox is the hard gate that lets us turn this kind on
safely.

Per the ``KindHandler`` contract this module is pure data → data: it
NEVER reads the live agent-card file. The diff is a JSON object with
``before`` (filled by the applier from the live card) + ``after`` (a
synthetic placeholder summarising why the cluster fired). The Rust
``EvolutionApplier`` resolves both halves at apply time. Keeping the
file lookup in the applier means this layer stays swappable and avoids
dragging the persona / agent-card crate into the engine's dependency
surface.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from corlinman_evolution_engine.clustering import SignalCluster
from corlinman_evolution_engine.proposals import EvolutionProposal, ProposalContext
from corlinman_evolution_engine.store import fetch_existing_targets

if TYPE_CHECKING:
    import aiosqlite

KIND_AGENT_CARD = "agent_card"

# Trigger event kinds. The observer emits ``agent.identity_drift`` when a
# self-evaluation pass flags the response voice as "no longer sounding
# like the character card", and ``agent.persona_misalignment`` when an
# operator flags an on-topic-guardrail breach. Both carry the agent name
# as ``target`` (matches the agents registry under
# ``[server].data_dir/agents/<name>.md``).
EVENT_AGENT_IDENTITY_DRIFT = "agent.identity_drift"
EVENT_AGENT_PERSONA_MISALIGNMENT = "agent.persona_misalignment"

AGENT_CARD_TRIGGERS: frozenset[str] = frozenset(
    {EVENT_AGENT_IDENTITY_DRIFT, EVENT_AGENT_PERSONA_MISALIGNMENT}
)

# Default threshold: at least this many trigger signals on the same
# agent in the engine's lookback window before a proposal fires. Higher
# than the prompt_template default because rewriting an agent card has
# wider blast radius than rewriting a single prompt segment.
DEFAULT_THRESHOLD = 4


def _build_diff(cluster: SignalCluster) -> str:
    """JSON-encoded ``{before, after, rationale}`` placeholder.

    The handler can't read the actual agent-card file (deliberately —
    the engine stays decoupled from the persona / agent-card crate).
    The applier fills the ``before`` field at apply time by reading
    the live card; we ship a synthetic ``after`` that summarises *why*
    the cluster fired so the operator gets a concrete starting point
    in the review UI instead of an empty editor.
    """
    target = cluster.target or "<unknown>"
    payload = {
        "before": "",  # populated by the applier from the live card.
        "after": (
            f"# TODO(operator): rewrite the {target!r} agent card to address "
            f"{cluster.size} {cluster.event_kind} signals observed in the "
            "last lookback window. Pay particular attention to the voice / "
            "tone section and the on-topic guardrails."
        ),
        "rationale": (
            f"agent-identity drift cluster of size {cluster.size} on agent "
            f"{target!r}"
        ),
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _reasoning_for(cluster: SignalCluster) -> str:
    """Human-readable ``reasoning`` for an agent-card proposal."""
    target = cluster.target or "<unknown>"
    sample_ids = ",".join(str(i) for i in cluster.signal_ids[:5])
    suffix = "..." if len(cluster.signal_ids) > 5 else ""
    return (
        f"agent {target!r} attracted {cluster.size} {cluster.event_kind} "
        f"signals (ids: {sample_ids}{suffix}); operator review of the "
        "character card recommended"
    )


class AgentCardHandler:
    """``KindHandler`` for the ``agent_card`` kind.

    Phase 4 W1 4-1D follow-up implementation: scan the engine's
    already-clustered signals for ``agent.identity_drift`` /
    ``agent.persona_misalignment`` clusters and emit one ``agent_card``
    proposal per agent whose cluster size meets ``threshold``.

    ``risk`` is always ``"high"`` because applying changes agent
    identity; the ShadowTester gating ensures these route through the
    docker sandbox before reaching the operator queue.

    ``threshold`` is configurable so a tighter deployment can require
    more evidence before proposing. The engine's global
    ``min_cluster_size`` still applies underneath; this is an
    additional floor for *this* kind specifically.

    ``existing_targets`` mirrors the dedup pattern from the other
    Phase 4 W1 4-1D handlers — a re-run on the same day with the same
    cluster does NOT emit duplicate proposals on the same agent.
    """

    def __init__(self, *, threshold: int = DEFAULT_THRESHOLD) -> None:
        if threshold < 1:
            raise ValueError(f"threshold must be >= 1, got {threshold}")
        self._threshold = threshold

    @property
    def kind(self) -> str:
        return KIND_AGENT_CARD

    @property
    def threshold(self) -> int:
        return self._threshold

    async def existing_targets(self, conn: object) -> set[tuple[str, str]]:
        sqlite_conn: aiosqlite.Connection = conn  # type: ignore[assignment]
        return await fetch_existing_targets(sqlite_conn, self.kind)

    async def propose(self, ctx: ProposalContext) -> list[EvolutionProposal]:
        relevant = [
            c
            for c in ctx.clusters
            if c.event_kind in AGENT_CARD_TRIGGERS
            and c.target
            and c.size >= self._threshold
        ]
        if not relevant:
            return []

        # Strongest cluster first — engine respects insertion order when
        # applying ``max_proposals_per_run`` so the loudest signal lands
        # before any silent ones.
        relevant.sort(key=lambda c: c.size, reverse=True)

        return [
            EvolutionProposal(
                kind=self.kind,
                target=cluster.target or "",
                diff=_build_diff(cluster),
                reasoning=_reasoning_for(cluster),
                risk="high",
                budget_cost=3,
                signal_ids=cluster.signal_ids,
                trace_ids=cluster.trace_ids,
                tenant_id=cluster.tenant_id,
            )
            for cluster in relevant
        ]
