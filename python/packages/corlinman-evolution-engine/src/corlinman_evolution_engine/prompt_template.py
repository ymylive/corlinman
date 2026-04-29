"""Generate ``prompt_template`` proposals from chat-quality signal clusters.

Phase 4 W1 4-1D: when chat output consistently misses intent for the same
template segment (e.g. ``agent.greeting`` keeps producing wooden replies),
the observer emits ``chat.intent_mismatch`` or
``agent.poor_response_quality`` events with the segment as ``target``. A
cluster of those past the threshold becomes a ``prompt_template`` proposal
asking the operator to rewrite that segment.

These proposals are reserved for ``risk = "high"`` because applying one
literally changes the agent's behaviour. The Phase 3 W1-A ShadowTester
already routes high-risk kinds through the shadow stage, and the Phase 4
W1 4-1C docker sandbox is the hard gate that lets us turn this kind on
safely (Phase 3 W2-C had to shelve it pending exactly that work).

Per the ``KindHandler`` contract this module is pure data → data: it
NEVER reads the live agent-card / prompt template file. The diff is a
JSON object with ``before`` (operator-supplied during the actual edit) +
``after`` (a placeholder we synthesise from the cluster). The Rust
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

KIND_PROMPT_TEMPLATE = "prompt_template"

# Trigger event kinds. The observer emits ``chat.intent_mismatch`` whenever
# a session ends with a thumbs-down / "you missed the point" reaction, and
# ``agent.poor_response_quality`` whenever a self-evaluation pass scores a
# response below the quality floor. Both carry the offending template
# segment as ``target`` (e.g. ``agent.greeting``, ``tool.web_search.system``).
EVENT_CHAT_INTENT_MISMATCH = "chat.intent_mismatch"
EVENT_AGENT_POOR_QUALITY = "agent.poor_response_quality"

PROMPT_TEMPLATE_TRIGGERS: frozenset[str] = frozenset(
    {EVENT_CHAT_INTENT_MISMATCH, EVENT_AGENT_POOR_QUALITY}
)

# Default threshold: at least this many trigger signals on the same
# segment in the engine's lookback window before a proposal fires.
# Mirrors the 7-day cluster detection wording in the roadmap; the engine
# still gates on ``min_cluster_size`` so this is a per-handler floor on
# top of the global one.
DEFAULT_THRESHOLD = 3


def _build_diff(cluster: SignalCluster) -> str:
    """JSON-encoded ``{before, after}`` placeholder.

    The handler can't read the actual prompt file (deliberately — the
    engine stays decoupled from the agent-card crate). The applier
    fills the ``before`` field at apply time by reading the live
    template; we ship a synthetic ``after`` that summarises *why* the
    cluster fired so the operator gets a concrete starting point in
    the review UI instead of an empty editor.
    """
    target = cluster.target or "<unknown>"
    payload = {
        "before": "",  # populated by the applier from the live template.
        "after": (
            f"# TODO(operator): rewrite the {target!r} segment to address "
            f"{cluster.size} {cluster.event_kind} signals observed in the "
            "last lookback window."
        ),
        "rationale": (
            f"intent-mismatch cluster of size {cluster.size} on segment "
            f"{target!r}"
        ),
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _reasoning_for(cluster: SignalCluster) -> str:
    """Human-readable ``reasoning`` for a prompt-template proposal."""
    target = cluster.target or "<unknown>"
    sample_ids = ",".join(str(i) for i in cluster.signal_ids[:5])
    suffix = "..." if len(cluster.signal_ids) > 5 else ""
    return (
        f"prompt template segment {target!r} attracted {cluster.size} "
        f"{cluster.event_kind} signals (ids: {sample_ids}{suffix}); "
        "operator review recommended"
    )


class PromptTemplateHandler:
    """``KindHandler`` for the ``prompt_template`` kind.

    Phase 4 W1 4-1D implementation: scan the engine's already-clustered
    signals for ``chat.intent_mismatch`` / ``agent.poor_response_quality``
    clusters and emit one ``prompt_template`` proposal per segment whose
    cluster size meets ``threshold``.

    ``risk`` is always ``"high"`` because applying changes agent
    behaviour; the ShadowTester gating ensures these route through the
    docker sandbox before reaching the operator queue.

    ``threshold`` is configurable so a tighter deployment can require
    more evidence before proposing. The engine's global
    ``min_cluster_size`` still applies underneath; this is an additional
    floor for *this* kind specifically.

    ``existing_targets`` mirrors the dedup pattern from
    ``MemoryOpHandler`` — a re-run on the same day with the same
    cluster does NOT emit duplicate proposals on the same segment.
    """

    def __init__(self, *, threshold: int = DEFAULT_THRESHOLD) -> None:
        if threshold < 1:
            raise ValueError(f"threshold must be >= 1, got {threshold}")
        self._threshold = threshold

    @property
    def kind(self) -> str:
        return KIND_PROMPT_TEMPLATE

    @property
    def threshold(self) -> int:
        return self._threshold

    async def existing_targets(self, conn: object) -> set[tuple[str, str]]:
        # Same cast pattern as the Phase 3 handlers. Keeping aiosqlite out
        # of the protocol surface lets test doubles stay trivial.
        sqlite_conn: aiosqlite.Connection = conn  # type: ignore[assignment]
        return await fetch_existing_targets(sqlite_conn, self.kind)

    async def propose(self, ctx: ProposalContext) -> list[EvolutionProposal]:
        relevant = [
            c
            for c in ctx.clusters
            if c.event_kind in PROMPT_TEMPLATE_TRIGGERS
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
