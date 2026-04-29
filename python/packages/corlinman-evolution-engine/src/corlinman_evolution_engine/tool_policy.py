"""Generate ``tool_policy`` proposals from approval / failure clusters.

Phase 4 W1 4-1D: when a single tool repeatedly triggers
``approval.denied`` / ``tool.unsafe_argument`` events, the operator is
implicitly asking us to tighten the policy on that tool. The opposite
direction — a tool that's been timing out *safely* often enough that
auto-mode would be a net win — is the loosen path. Both produce
``tool_policy`` proposals; the *direction* is encoded in the diff JSON.

These proposals are reserved for ``risk = "high"`` because applying one
expands or contracts the agent's tool surface — a direct behaviour
change. The Phase 3 W1-A ShadowTester routes high-risk kinds through
the shadow stage and the Phase 4 W1 4-1C docker sandbox is the hard
gate. Tightening (auto → prompt) fires more readily because the cost
of being wrong is "the operator gets prompted once per call"; loosening
(prompt → auto) requires more evidence because the cost of being wrong
is "the agent does something the operator wouldn't have approved".

Per the ``KindHandler`` contract this module is pure data → data: it
NEVER reads the live tool-policy file. The diff is a JSON object
``{before, after, rule_id}``. The Rust ``EvolutionApplier`` resolves
``before`` (the current mode) at apply time; we ship the proposed
``after`` mode and a synthetic rule id so the operator can look up
exactly which clause this proposal touches.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from corlinman_evolution_engine.clustering import SignalCluster
from corlinman_evolution_engine.proposals import EvolutionProposal, ProposalContext
from corlinman_evolution_engine.store import fetch_existing_targets

if TYPE_CHECKING:
    import aiosqlite

KIND_TOOL_POLICY = "tool_policy"

# Tighten triggers: signals where the operator (or a guard) said "no" to
# the tool. Either of these clustering on one tool means we should ask
# the operator whether to flip the tool to ``prompt`` mode.
EVENT_APPROVAL_DENIED = "approval.denied"
EVENT_TOOL_UNSAFE_ARGUMENT = "tool.unsafe_argument"

TIGHTEN_TRIGGERS: frozenset[str] = frozenset(
    {EVENT_APPROVAL_DENIED, EVENT_TOOL_UNSAFE_ARGUMENT}
)

# Loosen trigger: pure timeouts where the failure is "the tool didn't
# answer in time" rather than "the tool did something the operator
# disliked". Repeated timeouts under prompt mode mean the operator is
# spending interaction budget on something low-stakes; auto mode may be
# the right call. Loosening requires a higher threshold than tightening
# because the downside if we're wrong is bigger.
EVENT_TOOL_TIMEOUT = "tool.timeout"

LOOSEN_TRIGGERS: frozenset[str] = frozenset({EVENT_TOOL_TIMEOUT})

TOOL_POLICY_TRIGGERS: frozenset[str] = TIGHTEN_TRIGGERS | LOOSEN_TRIGGERS

# Default thresholds. Tightening is cheaper to undo so it fires sooner.
DEFAULT_TIGHTEN_THRESHOLD = 3
DEFAULT_LOOSEN_THRESHOLD = 5

# Mode names mirror the operator-facing strings in the approval policy
# config so an operator reading the proposal can map directly to the
# admin UI's policy editor.
MODE_PROMPT = "prompt"
MODE_AUTO = "auto"


def _direction_for(event_kind: str) -> str | None:
    """Tighten (`auto -> prompt`) vs loosen (`prompt -> auto`).

    Returns ``"tighten"`` / ``"loosen"`` / ``None``. ``None`` is "this
    event_kind isn't a trigger we recognise" — the handler filters those
    out before this is called, but we keep the function total so future
    extensions don't need to special-case it.
    """
    if event_kind in TIGHTEN_TRIGGERS:
        return "tighten"
    if event_kind in LOOSEN_TRIGGERS:
        return "loosen"
    return None


def _proposed_modes(direction: str) -> tuple[str, str]:
    """``(before, after)`` mode pair for a direction.

    The applier replaces ``before`` with the actual current mode at
    apply time; we ship the *expected* current mode here so the
    operator's UI can flag a mismatch (the policy may have already
    been changed manually since the cluster started forming).
    """
    if direction == "tighten":
        return MODE_AUTO, MODE_PROMPT
    return MODE_PROMPT, MODE_AUTO


def _rule_id(tool: str, direction: str) -> str:
    """Stable rule id used in the diff and the proposal's audit trail.

    Format: ``tool_policy.<tool>.<direction>``. Stable across runs so a
    re-run on the same tool produces the same id (the dedup key is
    ``target``, not the rule id, but the rule id needs to round-trip
    through the applier without collision).
    """
    return f"tool_policy.{tool}.{direction}"


def _build_diff(tool: str, direction: str) -> str:
    """JSON ``{before, after, rule_id}`` for the proposal."""
    before, after = _proposed_modes(direction)
    return json.dumps(
        {
            "before": before,
            "after": after,
            "rule_id": _rule_id(tool, direction),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _reasoning_for(cluster: SignalCluster, direction: str) -> str:
    """Human-readable ``reasoning`` field."""
    tool = cluster.target or "<unknown>"
    sample_ids = ",".join(str(i) for i in cluster.signal_ids[:5])
    suffix = "..." if len(cluster.signal_ids) > 5 else ""
    verb = (
        "tightening to 'prompt'"
        if direction == "tighten"
        else "loosening to 'auto'"
    )
    return (
        f"tool {tool!r} attracted {cluster.size} {cluster.event_kind} "
        f"signals (ids: {sample_ids}{suffix}); recommend {verb}"
    )


class ToolPolicyHandler:
    """``KindHandler`` for the ``tool_policy`` kind.

    Phase 4 W1 4-1D implementation: scan the engine's already-clustered
    signals for ``approval.denied`` / ``tool.unsafe_argument`` (tighten)
    or ``tool.timeout`` (loosen) clusters and emit one ``tool_policy``
    proposal per ``(tool, direction)`` pair. The two thresholds are
    independently configurable because tightening is cheap to undo and
    loosening isn't.

    ``risk`` is always ``"high"`` because applying expands/contracts the
    agent's tool surface; the ShadowTester gates these through docker
    sandbox before they reach the operator's queue.

    ``existing_targets`` mirrors the dedup pattern from the Phase 3
    handlers — once we've filed a proposal for ``web_search``,
    re-running the engine doesn't double-file even if the cluster grows
    further. The operator's decision (approve / deny) on the existing
    proposal is the right next step.
    """

    def __init__(
        self,
        *,
        tighten_threshold: int = DEFAULT_TIGHTEN_THRESHOLD,
        loosen_threshold: int = DEFAULT_LOOSEN_THRESHOLD,
    ) -> None:
        if tighten_threshold < 1:
            raise ValueError(
                f"tighten_threshold must be >= 1, got {tighten_threshold}"
            )
        if loosen_threshold < 1:
            raise ValueError(
                f"loosen_threshold must be >= 1, got {loosen_threshold}"
            )
        self._tighten_threshold = tighten_threshold
        self._loosen_threshold = loosen_threshold

    @property
    def kind(self) -> str:
        return KIND_TOOL_POLICY

    @property
    def tighten_threshold(self) -> int:
        return self._tighten_threshold

    @property
    def loosen_threshold(self) -> int:
        return self._loosen_threshold

    async def existing_targets(self, conn: object) -> set[tuple[str, str]]:
        sqlite_conn: aiosqlite.Connection = conn  # type: ignore[assignment]
        return await fetch_existing_targets(sqlite_conn, self.kind)

    async def propose(self, ctx: ProposalContext) -> list[EvolutionProposal]:
        candidates: list[tuple[SignalCluster, str]] = []
        for cluster in ctx.clusters:
            if not cluster.target:
                continue
            direction = _direction_for(cluster.event_kind)
            if direction is None:
                continue
            min_size = (
                self._tighten_threshold
                if direction == "tighten"
                else self._loosen_threshold
            )
            if cluster.size < min_size:
                continue
            candidates.append((cluster, direction))

        if not candidates:
            return []

        # Strongest cluster first — engine respects insertion order when
        # applying ``max_proposals_per_run``. Tighten clusters with the
        # same size as a loosen cluster sort first because tightening is
        # the safer direction.
        def _sort_key(item: tuple[SignalCluster, str]) -> tuple[int, int]:
            cluster, direction = item
            # Negative size for descending; tighten before loosen.
            return (-cluster.size, 0 if direction == "tighten" else 1)

        candidates.sort(key=_sort_key)

        return [
            EvolutionProposal(
                kind=self.kind,
                target=cluster.target or "",
                diff=_build_diff(cluster.target or "", direction),
                reasoning=_reasoning_for(cluster, direction),
                risk="high",
                budget_cost=3,
                signal_ids=cluster.signal_ids,
                trace_ids=cluster.trace_ids,
                tenant_id=cluster.tenant_id,
            )
            for cluster, direction in candidates
        ]
