//! Public types for the EvolutionLoop. These live in their own module so
//! both the gateway (observer + admin API) and external consumers (Python
//! engine via SQLite, future tools) can pin to the same shape.
//!
//! Times are stored as unix milliseconds (i64). JSON payload fields are
//! kept as `serde_json::Value` so we can evolve them without bumping the
//! schema.

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use std::str::FromStr;

/// Strongly-typed proposal id, e.g. `evol-2026-04-24-001`.
#[derive(Debug, Clone, Eq, PartialEq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct ProposalId(pub String);

impl ProposalId {
    pub fn new(s: impl Into<String>) -> Self {
        Self(s.into())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Display for ProposalId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

// ---------------------------------------------------------------------------
// Enums — serialized as snake_case strings to match the SQL TEXT columns.
// ---------------------------------------------------------------------------

// `Ord`/`PartialOrd` so `BTreeMap<EvolutionKind, _>` (the W1-C budget map
// in `corlinman-core::config`) sorts deterministically. `JsonSchema` so
// the same type can appear in `Config`'s schema export.
#[derive(
    Debug, Clone, Copy, Eq, PartialEq, Hash, Ord, PartialOrd, Serialize, Deserialize, JsonSchema,
)]
#[serde(rename_all = "snake_case")]
pub enum EvolutionKind {
    MemoryOp,
    TagRebalance,
    RetryTuning,
    AgentCard,
    SkillUpdate,
    PromptTemplate,
    ToolPolicy,
    NewSkill,
    // ─── Phase 4 W2 B1 — meta proposal kinds (engine modifies engine) ──
    //
    // Per `docs/design/phase4-roadmap.md` §3 Wave 2 row 4-2A. These four
    // kinds represent the EvolutionEngine targeting its own configuration
    // surface. They route to a separate `meta_pending` queue with stricter
    // approval rules; recursion guard (iter 3+) prevents a meta-proposal
    // from itself spawning meta-proposals; operator-only capability gate
    // (iter 5+) keeps these out of regular reviewer hands.
    //
    // The payload that describes *what* gets modified rides in the
    // existing `EvolutionProposal.target` + `EvolutionProposal.diff`
    // fields — same pattern `memory_op` uses (target = `"merge_chunks:42,43"`,
    // empty diff). The sibling `meta::*` structs below define the
    // canonical JSON shapes the engine + applier (iter 4) will agree on.
    EngineConfig,
    EnginePrompt,
    ObserverFilter,
    ClusterThreshold,
}

impl EvolutionKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::MemoryOp => "memory_op",
            Self::TagRebalance => "tag_rebalance",
            Self::RetryTuning => "retry_tuning",
            Self::AgentCard => "agent_card",
            Self::SkillUpdate => "skill_update",
            Self::PromptTemplate => "prompt_template",
            Self::ToolPolicy => "tool_policy",
            Self::NewSkill => "new_skill",
            Self::EngineConfig => "engine_config",
            Self::EnginePrompt => "engine_prompt",
            Self::ObserverFilter => "observer_filter",
            Self::ClusterThreshold => "cluster_threshold",
        }
    }

    /// `true` for the four Phase 4 W2 B1 meta kinds where the
    /// EvolutionEngine targets its own configuration / prompts /
    /// observer / clustering thresholds. Used by the recursion guard
    /// (iter 3+) and the operator-only capability gate (iter 5+) to
    /// branch on routing without re-listing the variants.
    ///
    /// Exhaustive match — adding a new variant forces the author to
    /// classify it here, which is the whole point.
    pub fn is_meta(&self) -> bool {
        match self {
            Self::MemoryOp
            | Self::TagRebalance
            | Self::RetryTuning
            | Self::AgentCard
            | Self::SkillUpdate
            | Self::PromptTemplate
            | Self::ToolPolicy
            | Self::NewSkill => false,
            Self::EngineConfig
            | Self::EnginePrompt
            | Self::ObserverFilter
            | Self::ClusterThreshold => true,
        }
    }
}

impl FromStr for EvolutionKind {
    type Err = ParseError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Ok(match s {
            "memory_op" => Self::MemoryOp,
            "tag_rebalance" => Self::TagRebalance,
            "retry_tuning" => Self::RetryTuning,
            "agent_card" => Self::AgentCard,
            "skill_update" => Self::SkillUpdate,
            "prompt_template" => Self::PromptTemplate,
            "tool_policy" => Self::ToolPolicy,
            "new_skill" => Self::NewSkill,
            "engine_config" => Self::EngineConfig,
            "engine_prompt" => Self::EnginePrompt,
            "observer_filter" => Self::ObserverFilter,
            "cluster_threshold" => Self::ClusterThreshold,
            other => return Err(ParseError::UnknownKind(other.into())),
        })
    }
}

// ---------------------------------------------------------------------------
// Phase 4 W2 B1 — meta proposal payload shapes.
//
// The engine writes these as JSON into `EvolutionProposal.diff`; iter 4
// (the EvolutionApplier extension) decodes them and dispatches to the
// engine-config / prompt / filter / threshold mutators. They live here
// rather than in `corlinman-gateway` because the Python EvolutionEngine
// also speaks this contract — same cross-language rationale that put the
// existing `EvolutionKind` strings in this crate.
//
// Each payload includes the existing value + the proposed value so the
// applier can build `inverse_diff` (for rollback) and the operator-
// review UI can render a clean before/after without a separate read.
// `reason` is a short human string that gets surfaced in the meta queue.
//
// These are intentionally minimal. The roadmap doesn't pin field-by-field
// shapes; iter 2 (engine handlers) is where the engine validates them
// against the live config / prompt store and can extend the structs (e.g.
// `min_value` / `max_value` ranges for thresholds) without breaking
// existing rows because `serde(deny_unknown_fields)` is intentionally
// **not** set — older payloads remain decodable.
// ---------------------------------------------------------------------------

pub mod meta {
    use serde::{Deserialize, Serialize};

    /// Shape for `EvolutionKind::EngineConfig` proposals.
    /// `target` on the proposal is the dotted config path
    /// (e.g. `"evolution.budget.weekly_total"`); the JSON value before/
    /// after lets the engine apply + revert without re-resolving the
    /// path under a config that may have shifted between propose and
    /// apply time.
    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    pub struct EngineConfigPayload {
        pub config_path: String,
        pub previous_value: serde_json::Value,
        pub proposed_value: serde_json::Value,
        pub reason: String,
    }

    /// Shape for `EvolutionKind::EnginePrompt` proposals — rewrites of
    /// the engine's own clustering / proposal-generation prompts. The
    /// roadmap calls out `engine_prompt` as the canonical
    /// double-confirm kind, so the applier reads these strings raw
    /// without any templating; the diff is the operator's review surface.
    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    pub struct EnginePromptPayload {
        /// Stable identifier for which engine prompt this targets, e.g.
        /// `"clustering"`, `"proposal_generation"`. Engine owns the set;
        /// unknown ids fail the iter-4 applier, not this layer.
        pub prompt_id: String,
        pub previous_text: String,
        pub proposed_text: String,
        pub reason: String,
    }

    /// Shape for `EvolutionKind::ObserverFilter` proposals — adjustments
    /// to which `evolution_signals.event_kind` rows the EvolutionObserver
    /// keeps vs. drops. `previous_filter` / `proposed_filter` are
    /// opaque JSON so the engine can ship richer filter DSLs later
    /// without a schema bump on this struct.
    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    pub struct ObserverFilterPayload {
        /// e.g. `"tool.call.failed"` or a glob like `"tool.call.*"` —
        /// engine decides; this layer is just the wire shape.
        pub event_kind_pattern: String,
        pub previous_filter: serde_json::Value,
        pub proposed_filter: serde_json::Value,
        pub reason: String,
    }

    /// Shape for `EvolutionKind::ClusterThreshold` proposals — the
    /// signal-clustering hyperparameters the engine uses to fold raw
    /// signals into proposal candidates. f64 because every threshold
    /// in the engine today is a float (similarity cutoff, severity
    /// weight, time-window decay).
    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    pub struct ClusterThresholdPayload {
        /// Stable name owned by the engine (e.g. `"min_similarity"`,
        /// `"min_signals_per_cluster"`). Engine validates the name;
        /// this struct only pins the wire format.
        pub threshold_name: String,
        pub previous_value: f64,
        pub proposed_value: f64,
        pub reason: String,
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EvolutionRisk {
    Low,
    Medium,
    High,
}

impl EvolutionRisk {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Low => "low",
            Self::Medium => "medium",
            Self::High => "high",
        }
    }
}

impl FromStr for EvolutionRisk {
    type Err = ParseError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Ok(match s {
            "low" => Self::Low,
            "medium" => Self::Medium,
            "high" => Self::High,
            other => return Err(ParseError::UnknownRisk(other.into())),
        })
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EvolutionStatus {
    Pending,
    ShadowRunning,
    ShadowDone,
    Approved,
    Denied,
    Applied,
    RolledBack,
}

impl EvolutionStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::ShadowRunning => "shadow_running",
            Self::ShadowDone => "shadow_done",
            Self::Approved => "approved",
            Self::Denied => "denied",
            Self::Applied => "applied",
            Self::RolledBack => "rolled_back",
        }
    }
}

impl FromStr for EvolutionStatus {
    type Err = ParseError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Ok(match s {
            "pending" => Self::Pending,
            "shadow_running" => Self::ShadowRunning,
            "shadow_done" => Self::ShadowDone,
            "approved" => Self::Approved,
            "denied" => Self::Denied,
            "applied" => Self::Applied,
            "rolled_back" => Self::RolledBack,
            other => return Err(ParseError::UnknownStatus(other.into())),
        })
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SignalSeverity {
    Info,
    Warn,
    Error,
}

impl SignalSeverity {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Info => "info",
            Self::Warn => "warn",
            Self::Error => "error",
        }
    }
}

impl FromStr for SignalSeverity {
    type Err = ParseError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Ok(match s {
            "info" => Self::Info,
            "warn" => Self::Warn,
            "error" => Self::Error,
            other => return Err(ParseError::UnknownSeverity(other.into())),
        })
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ParseError {
    #[error("unknown evolution kind: {0}")]
    UnknownKind(String),
    #[error("unknown risk: {0}")]
    UnknownRisk(String),
    #[error("unknown status: {0}")]
    UnknownStatus(String),
    #[error("unknown severity: {0}")]
    UnknownSeverity(String),
}

// ---------------------------------------------------------------------------
// Row types — mirror the SQLite tables 1:1 for the most part. Repos are
// responsible for the JSON ↔ String conversions on signal_ids / trace_ids /
// metrics_baseline / shadow_metrics.
// ---------------------------------------------------------------------------

/// One observed event candidate for evolution. Written by the gateway's
/// EvolutionObserver as hooks fire.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EvolutionSignal {
    /// `None` for rows about to be inserted (autoincrement assigns).
    pub id: Option<i64>,
    pub event_kind: String,
    pub target: Option<String>,
    pub severity: SignalSeverity,
    pub payload_json: serde_json::Value,
    pub trace_id: Option<String>,
    pub session_id: Option<String>,
    /// Unix milliseconds.
    pub observed_at: i64,
    /// Phase 4 W1 4-1A: tenant the signal belongs to. Defaults to
    /// `"default"` for the legacy single-tenant deployment shape and
    /// for events whose source (chat hooks today) doesn't yet carry
    /// tenant context. The schema column has the same default so old
    /// code paths that omit this field still produce a valid row;
    /// the explicit field here is the path forward.
    #[serde(default = "default_tenant")]
    pub tenant_id: String,
}

fn default_tenant() -> String {
    "default".to_string()
}

/// A proposed mutation to a corlinman asset. Generated by the Python
/// EvolutionEngine, queued in `evolution_proposals`, surfaced through
/// `/admin/evolution`, applied on approval.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EvolutionProposal {
    pub id: ProposalId,
    pub kind: EvolutionKind,
    pub target: String,
    /// Unified diff (as a single string). Empty for `memory_op` kinds where
    /// the operation is encoded in `reasoning` + `target` (e.g.
    /// `merge_chunks:42,43`).
    pub diff: String,
    pub reasoning: String,
    pub risk: EvolutionRisk,
    pub budget_cost: u32,
    pub status: EvolutionStatus,
    pub shadow_metrics: Option<ShadowMetrics>,
    pub signal_ids: Vec<i64>,
    pub trace_ids: Vec<String>,
    /// Unix milliseconds.
    pub created_at: i64,
    pub decided_at: Option<i64>,
    pub decided_by: Option<String>,
    pub applied_at: Option<i64>,
    pub rollback_of: Option<ProposalId>,
    // ─── W1-A: shadow run identifiers ─────────────────────────────────
    /// Identifier of the eval run that produced `shadow_metrics`.
    pub eval_run_id: Option<String>,
    /// Pre-shadow baseline counts captured by the ShadowTester. Mirrors
    /// the same MetricSnapshot shape the W1-B applier writes into
    /// `evolution_history.metrics_baseline`.
    pub baseline_metrics_json: Option<serde_json::Value>,
    // ─── W1-B: auto-rollback audit ────────────────────────────────────
    /// Unix-millis timestamp the AutoRollback monitor flipped this row
    /// from `applied → rolled_back`. None if the proposal was never
    /// auto-rolled (manual rollback uses a fresh proposal with
    /// `rollback_of` set).
    pub auto_rollback_at: Option<i64>,
    /// Human-readable summary of the threshold breach that triggered the
    /// auto-rollback (e.g. `"err_signal_count: 4 -> 12 (+200%)"`).
    pub auto_rollback_reason: Option<String>,
    // ─── Phase 4 W2: free-form metadata blob ──────────────────────────
    /// Free-form JSON blob keyed by feature. Stored as TEXT in SQLite
    /// (NULL when None, JSON-encoded otherwise) so new surfaces can
    /// stash typed views without schema churn. Current consumers:
    ///
    /// - **B1 (meta proposal recursion guard)** reads
    ///   `metadata.parent_meta_proposal_id` + `metadata.descended_from`
    ///   to walk the trace_id descent chain and refuse runaway
    ///   self-mutation.
    /// - **B3 (per-tenant federation)** reads
    ///   `metadata.federated_from = { tenant, source_proposal_id, hop }`
    ///   to detect inbound federated proposals and stamp a hop counter
    ///   for asymmetric opt-in routing.
    ///
    /// Unknown keys must round-trip untouched — both surfaces only
    /// reach into their own namespace, never overwriting each other.
    pub metadata: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ShadowMetrics {
    /// Free-form JSON; engine and ShadowTester decide the shape per kind.
    /// Common keys: `success_rate`, `p95_latency_ms`, `avg_cost_usd`.
    #[serde(flatten)]
    pub data: serde_json::Map<String, serde_json::Value>,
}

/// One applied (or rolled-back) proposal. Read-mostly audit log.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EvolutionHistory {
    pub id: Option<i64>,
    pub proposal_id: ProposalId,
    pub kind: EvolutionKind,
    pub target: String,
    pub before_sha: String,
    pub after_sha: String,
    pub inverse_diff: String,
    pub metrics_baseline: serde_json::Value,
    pub applied_at: i64,
    pub rolled_back_at: Option<i64>,
    pub rollback_reason: Option<String>,
}
