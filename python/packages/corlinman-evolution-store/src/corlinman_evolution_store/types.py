"""Public types for the EvolutionLoop.

Ported 1:1 from ``rust/crates/corlinman-evolution/src/types.rs``. Times are
stored as unix milliseconds (``int``). JSON payload fields stay typed as
``dict | list | str | int | float | bool | None`` (the JSON document
model) so we can evolve them without bumping the schema.

Enum string values match the Rust ``serde(rename_all = "snake_case")``
output exactly — they are the cross-language wire format with the Rust
admin API and the SQLite TEXT columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, NewType

# Free-form JSON node. We keep this loose (``Any``) rather than wrestling
# a recursive type alias through mypy — the repo never inspects the
# structure, only serialises through ``json.dumps`` / ``json.loads``.
Json = Any

DEFAULT_TENANT_ID: str = "default"


# ---------------------------------------------------------------------------
# Curator-loop signal kinds (Phase 4 W4 — hermes port).
#
# These are the cross-language wire values written into
# ``evolution_signals.event_kind``. Kept as module-level ``str`` constants
# (not an Enum) so they coexist with the existing free-form ``event_kind``
# convention — the gateway hooks already emit ``engine.run.completed`` /
# ``tool.call.failed`` / etc. as ad-hoc strings.
# ---------------------------------------------------------------------------

EVENT_USER_CORRECTION: str = "user.correction"
"""Signal emitted when a user-correction-detector spots a corrective
phrase in chat (e.g. ``"stop"``, ``"don't do that"``). ``target`` should
be the skill name when one is implicated, otherwise the profile slug."""

EVENT_SKILL_UNUSED: str = "skill.unused"
"""Curator detected a skill has gone unused past the stale threshold.
Payload carries the new lifecycle state (``"stale"`` or ``"archived"``)."""

EVENT_IDLE_REFLECTION: str = "idle.reflection"
"""Background curator loop fired because the inactivity interval elapsed.
``target`` = profile slug. Payload carries the ``CuratorReport``
(counts of ``marked_stale``, ``archived``, ``reactivated``)."""

EVENT_CURATOR_RUN_COMPLETED: str = "curator.run.completed"
"""Curator-loop run finished cleanly — analogous to the existing
``engine.run.completed``."""

EVENT_CURATOR_RUN_FAILED: str = "curator.run.failed"
"""Curator-loop run aborted with an error — analogous to the existing
``engine.run.failed``."""


# Strongly-typed proposal id. NewType so the type checker keeps it
# distinct from a plain ``str`` while letting it serialise the same way.
ProposalId = NewType("ProposalId", str)


# ---------------------------------------------------------------------------
# Errors raised on bad enum / JSON values from the DB. Kept tiny so the
# caller surface stays simple.
# ---------------------------------------------------------------------------


class ParseError(ValueError):
    """Raised when a snake_case string cannot be parsed as one of the
    EvolutionLoop enums. Mirrors the Rust ``ParseError`` variants."""


# ---------------------------------------------------------------------------
# Enums — serialised as snake_case strings to match the SQL TEXT columns.
# ---------------------------------------------------------------------------


class EvolutionKind(str, Enum):
    """The 12 proposal kinds. First 8 are agent-asset proposals; the last
    4 (engine_*, observer_filter, cluster_threshold) are *meta* — the
    engine targets its own configuration."""

    MEMORY_OP = "memory_op"
    TAG_REBALANCE = "tag_rebalance"
    RETRY_TUNING = "retry_tuning"
    AGENT_CARD = "agent_card"
    SKILL_UPDATE = "skill_update"
    PROMPT_TEMPLATE = "prompt_template"
    TOOL_POLICY = "tool_policy"
    NEW_SKILL = "new_skill"
    # Phase 4 W2 B1 meta kinds (engine modifies engine).
    ENGINE_CONFIG = "engine_config"
    ENGINE_PROMPT = "engine_prompt"
    OBSERVER_FILTER = "observer_filter"
    CLUSTER_THRESHOLD = "cluster_threshold"

    def as_str(self) -> str:
        return self.value

    def is_meta(self) -> bool:
        """``True`` for the four Phase 4 W2 B1 meta kinds where the
        EvolutionEngine targets its own configuration / prompts /
        observer / clustering thresholds."""
        return self in (
            EvolutionKind.ENGINE_CONFIG,
            EvolutionKind.ENGINE_PROMPT,
            EvolutionKind.OBSERVER_FILTER,
            EvolutionKind.CLUSTER_THRESHOLD,
        )

    @classmethod
    def from_str(cls, value: str) -> EvolutionKind:
        try:
            return cls(value)
        except ValueError as exc:
            raise ParseError(f"unknown evolution kind: {value}") from exc


class EvolutionRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    def as_str(self) -> str:
        return self.value

    @classmethod
    def from_str(cls, value: str) -> EvolutionRisk:
        try:
            return cls(value)
        except ValueError as exc:
            raise ParseError(f"unknown risk: {value}") from exc


class EvolutionStatus(str, Enum):
    PENDING = "pending"
    SHADOW_RUNNING = "shadow_running"
    SHADOW_DONE = "shadow_done"
    APPROVED = "approved"
    DENIED = "denied"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"

    def as_str(self) -> str:
        return self.value

    @classmethod
    def from_str(cls, value: str) -> EvolutionStatus:
        try:
            return cls(value)
        except ValueError as exc:
            raise ParseError(f"unknown status: {value}") from exc


class SignalSeverity(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"

    def as_str(self) -> str:
        return self.value

    @classmethod
    def from_str(cls, value: str) -> SignalSeverity:
        try:
            return cls(value)
        except ValueError as exc:
            raise ParseError(f"unknown severity: {value}") from exc


# ---------------------------------------------------------------------------
# Meta proposal payload shapes (Phase 4 W2 B1).
#
# The engine writes these as JSON into ``EvolutionProposal.diff``; iter 4
# (the EvolutionApplier extension) decodes them and dispatches to the
# engine-config / prompt / filter / threshold mutators.
# ---------------------------------------------------------------------------


@dataclass
class EngineConfigPayload:
    """Shape for ``EvolutionKind.ENGINE_CONFIG`` proposals."""

    config_path: str
    previous_value: Json
    proposed_value: Json
    reason: str


@dataclass
class EnginePromptPayload:
    """Shape for ``EvolutionKind.ENGINE_PROMPT`` proposals."""

    prompt_id: str
    previous_text: str
    proposed_text: str
    reason: str


@dataclass
class ObserverFilterPayload:
    """Shape for ``EvolutionKind.OBSERVER_FILTER`` proposals."""

    event_kind_pattern: str
    previous_filter: Json
    proposed_filter: Json
    reason: str


@dataclass
class ClusterThresholdPayload:
    """Shape for ``EvolutionKind.CLUSTER_THRESHOLD`` proposals."""

    threshold_name: str
    previous_value: float
    proposed_value: float
    reason: str


# ---------------------------------------------------------------------------
# Row types — mirror the SQLite tables 1:1.
# ---------------------------------------------------------------------------


@dataclass
class EvolutionSignal:
    """One observed event candidate for evolution. Written by the gateway's
    EvolutionObserver as hooks fire."""

    event_kind: str
    severity: SignalSeverity
    payload_json: Json
    observed_at: int
    """Unix milliseconds."""
    id: int | None = None
    target: str | None = None
    trace_id: str | None = None
    session_id: str | None = None
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass
class ShadowMetrics:
    """Free-form JSON metrics. Engine + ShadowTester decide the shape per
    kind. Common keys: ``success_rate``, ``p95_latency_ms``, ``avg_cost_usd``."""

    data: dict[str, Json] = field(default_factory=dict)


@dataclass
class EvolutionProposal:
    """A proposed mutation to a corlinman asset. Generated by the Python
    EvolutionEngine, queued in ``evolution_proposals``, surfaced through
    ``/admin/evolution``, applied on approval."""

    id: ProposalId
    kind: EvolutionKind
    target: str
    diff: str
    """Unified diff (single string). Empty for ``memory_op`` kinds."""
    reasoning: str
    risk: EvolutionRisk
    budget_cost: int
    status: EvolutionStatus
    created_at: int
    """Unix milliseconds."""
    signal_ids: list[int] = field(default_factory=list)
    trace_ids: list[str] = field(default_factory=list)
    shadow_metrics: ShadowMetrics | None = None
    decided_at: int | None = None
    decided_by: str | None = None
    applied_at: int | None = None
    rollback_of: ProposalId | None = None
    # W1-A shadow run identifiers.
    eval_run_id: str | None = None
    baseline_metrics_json: Json | None = None
    # W1-B auto-rollback audit.
    auto_rollback_at: int | None = None
    auto_rollback_reason: str | None = None
    # Phase 4 W2 free-form metadata blob.
    metadata: Json | None = None


@dataclass
class EvolutionHistory:
    """One applied (or rolled-back) proposal. Read-mostly audit log."""

    proposal_id: ProposalId
    kind: EvolutionKind
    target: str
    before_sha: str
    after_sha: str
    inverse_diff: str
    metrics_baseline: Json
    applied_at: int
    id: int | None = None
    rolled_back_at: int | None = None
    rollback_reason: str | None = None
    # Phase 4 W2 B3 iter 3 — JSON-encoded peer slug array (None when not federated).
    share_with: list[str] | None = None


__all__ = [
    "DEFAULT_TENANT_ID",
    "EVENT_CURATOR_RUN_COMPLETED",
    "EVENT_CURATOR_RUN_FAILED",
    "EVENT_IDLE_REFLECTION",
    "EVENT_SKILL_UNUSED",
    "EVENT_USER_CORRECTION",
    "ClusterThresholdPayload",
    "EngineConfigPayload",
    "EnginePromptPayload",
    "EvolutionHistory",
    "EvolutionKind",
    "EvolutionProposal",
    "EvolutionRisk",
    "EvolutionSignal",
    "EvolutionStatus",
    "Json",
    "ObserverFilterPayload",
    "ParseError",
    "ProposalId",
    "ShadowMetrics",
    "SignalSeverity",
]
