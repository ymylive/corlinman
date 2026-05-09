//! `HookEvent` — the wire-stable enum broadcast on the hook bus.
//!
//! Serialization uses `#[serde(tag = "kind")]` so JSON payloads carry an
//! explicit discriminant field and can evolve independently of Rust field
//! order. Consumers outside Rust (Python bridge, gateway admin UI) depend
//! on that stability.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind")]
pub enum HookEvent {
    MessageReceived {
        channel: String,
        session_key: String,
        content: String,
        metadata: serde_json::Value,
        /// Phase 4 W2 B2 iter 8: canonical user_id resolved via the
        /// `IdentityStore` from `(channel, channel_user_id)`. `None`
        /// when the channel adapter / chat handler hasn't been wired
        /// to the resolver yet — downstream consumers (persona,
        /// evolution observer) treat absent as "fall back to legacy
        /// per-channel-key attribution".
        #[serde(default, skip_serializing_if = "Option::is_none")]
        user_id: Option<String>,
    },
    MessageSent {
        channel: String,
        session_key: String,
        content: String,
        success: bool,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        user_id: Option<String>,
    },
    MessageTranscribed {
        session_key: String,
        transcript: String,
        media_path: String,
        media_type: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        user_id: Option<String>,
    },
    MessagePreprocessed {
        session_key: String,
        transcript: String,
        is_group: bool,
        group_id: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        user_id: Option<String>,
    },
    SessionPatch {
        session_key: String,
        patch: serde_json::Value,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        user_id: Option<String>,
    },
    AgentBootstrap {
        workspace_dir: String,
        session_key: String,
        files: Vec<String>,
    },
    GatewayStartup {
        version: String,
    },
    ConfigChanged {
        section: String,
        old: serde_json::Value,
        new: serde_json::Value,
    },
    /// Emitted once a remote/local tool invocation settles. `ok == false`
    /// implies `error_code` is populated with a short machine-readable
    /// identifier (e.g. `"timeout"`, `"disconnected"`, `"unsupported"`).
    ///
    /// Phase 4 W1.5 (next-tasks A1): `tenant_id` carries the source
    /// tenant when the chat / tool path knows it. `None` falls back
    /// to `default` when the EvolutionObserver persists the resulting
    /// signal — matching the schema column default. Wider tenant
    /// context propagation through the chat lifecycle is a follow-up;
    /// this field is the wire-shape commitment.
    ToolCalled {
        tool: String,
        runner_id: String,
        duration_ms: u64,
        ok: bool,
        error_code: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        tenant_id: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        user_id: Option<String>,
    },
    /// An approval request was raised; admins may see it via SSE or the UI.
    ///
    /// `timeout_at_ms` is wall-clock milliseconds since the Unix epoch: the
    /// deadline after which the gate will auto-resolve to `timeout`.
    /// `args_preview` is truncated on the emitting side so the bus doesn't
    /// fan out large JSON payloads.
    ApprovalRequested {
        id: String,
        session_key: String,
        plugin: String,
        tool: String,
        args_preview: String,
        timeout_at_ms: u64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        user_id: Option<String>,
    },
    /// Administrator (or auto-timeout) decided an approval.
    ///
    /// `decision` is one of `"allow"`, `"deny"`, or `"timeout"`. `decider`
    /// carries the operator identity for `allow`/`deny` and is `None` for
    /// `timeout`.
    ///
    /// Phase 4 W1.5 (next-tasks A1): `tenant_id` carries the source
    /// tenant when the approval gate knows it. See `ToolCalled` for
    /// the legacy default semantics.
    ApprovalDecided {
        id: String,
        decision: String,
        decider: Option<String>,
        decided_at_ms: u64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        tenant_id: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        user_id: Option<String>,
    },
    /// Rate-limit rejected a request.
    ///
    /// `limit_type` identifies which dimension tripped (e.g. `"channel_qq"`,
    /// `"channel_telegram"`, `"tool"`). `retry_after_ms` is a coarse hint
    /// (0 when no clean retry window is known).
    RateLimitTriggered {
        session_key: String,
        limit_type: String,
        retry_after_ms: u64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        user_id: Option<String>,
    },
    /// A NodeBridge-connected device emitted a telemetry metric.
    ///
    /// `node_id` identifies the remote device, `metric` is the metric name
    /// (dot-separated path; no schema enforced at the bus layer), and
    /// `tags` carries the metric's dimensional labels. Kept `BTreeMap` so
    /// the serialized JSON key order is stable across emits — matters for
    /// golden-file based tests downstream.
    Telemetry {
        node_id: String,
        metric: String,
        value: f64,
        tags: BTreeMap<String, String>,
    },
    /// The `corlinman-scheduler` ran a `JobAction::Subprocess` job whose
    /// child exited 0 within the configured timeout. Phase 2 wave 2-B uses
    /// this for the `evolution_engine` daily job; the `EvolutionObserver`
    /// folds it into `evolution_signals` so a successful run shows up as
    /// input to the *next* run — closing the loop.
    EngineRunCompleted {
        run_id: String,
        proposals_generated: u64,
        duration_ms: u64,
    },
    /// The `corlinman-scheduler` ran a `JobAction::Subprocess` job that
    /// either exited non-zero or was killed by the runtime's timeout
    /// guard. `error_kind` is one of `"exit_code"`, `"timeout"`,
    /// `"spawn_failed"`. `exit_code` is `None` for spawn / timeout cases.
    EngineRunFailed {
        run_id: String,
        error_kind: String,
        exit_code: Option<i32>,
    },
    /// Phase 4 W4 D3 iter 9: a child subagent was admitted by the
    /// supervisor and is about to start running. Mirrors the shape of
    /// `EngineRunCompleted/Failed` so downstream observers can pattern-
    /// match the four `Subagent*` variants the same way.
    ///
    /// `parent_trace_id` is the child's link back to its parent's trace
    /// tree — the evolution observer's join query (design § "Open
    /// question 4") uses it to fold child signals into the same cluster
    /// as the parent's. The child also gets its own `trace_id` (which
    /// may be the same string when the parent didn't bump it; the
    /// supervisor never mints a new id, just passes through).
    SubagentSpawned {
        parent_session_key: String,
        child_session_key: String,
        child_agent_id: String,
        agent_card: String,
        depth: u8,
        parent_trace_id: String,
        tenant_id: String,
    },
    /// The child reasoning loop returned cleanly (any of `Stop`,
    /// `Length`, `Error` — `Timeout` and the pre-spawn rejections have
    /// dedicated variants for clarity).
    ///
    /// `finish_reason` carries the underlying `FinishReason` snake_case
    /// value verbatim so the operator UI / evolution adapter can branch
    /// without re-importing the enum.
    SubagentCompleted {
        parent_session_key: String,
        child_session_key: String,
        child_agent_id: String,
        finish_reason: String,
        elapsed_ms: u64,
        tool_calls_made: u32,
        parent_trace_id: String,
        tenant_id: String,
    },
    /// The child's wall-clock budget expired and the cooperative cancel
    /// path fired. Distinct variant (rather than a `Completed` with
    /// `finish_reason="timeout"`) so dashboards can red-flag timeouts
    /// without parsing the inner field. `elapsed_ms` is the *actual*
    /// time spent before the cancel — typically `≈ max_wall_seconds`.
    SubagentTimedOut {
        parent_session_key: String,
        child_session_key: String,
        child_agent_id: String,
        elapsed_ms: u64,
        parent_trace_id: String,
        tenant_id: String,
    },
    /// The supervisor refused the spawn at depth-cap check time. No
    /// `child_session_key` because the slot was never allocated; the
    /// `parent_session_key` + `attempted_depth` pair are enough to
    /// identify the would-be spawn.
    ///
    /// Concurrency / tenant-quota rejections share this variant with
    /// `reason` discriminating; matches the design's "all four caps emit
    /// hook events on rejection" wording (§ "Resource governance"). The
    /// `reason` strings are `"depth_capped"`, `"parent_concurrency_exceeded"`,
    /// `"tenant_quota_exceeded"` — the same lowercase shape the supervisor's
    /// own `AcquireReject::Display` produces.
    SubagentDepthCapped {
        parent_session_key: String,
        attempted_depth: u8,
        reason: String,
        parent_trace_id: String,
        tenant_id: String,
    },
}

impl HookEvent {
    /// Short, stable discriminant string. Used as a tracing span field so
    /// traces can be filtered by event kind without parsing the enum.
    pub fn kind(&self) -> &'static str {
        match self {
            Self::MessageReceived { .. } => "message_received",
            Self::MessageSent { .. } => "message_sent",
            Self::MessageTranscribed { .. } => "message_transcribed",
            Self::MessagePreprocessed { .. } => "message_preprocessed",
            Self::SessionPatch { .. } => "session_patch",
            Self::AgentBootstrap { .. } => "agent_bootstrap",
            Self::GatewayStartup { .. } => "gateway_startup",
            Self::ConfigChanged { .. } => "config_changed",
            Self::ToolCalled { .. } => "tool_called",
            Self::ApprovalRequested { .. } => "approval_requested",
            Self::ApprovalDecided { .. } => "approval_decided",
            Self::RateLimitTriggered { .. } => "rate_limit_triggered",
            Self::Telemetry { .. } => "telemetry",
            Self::EngineRunCompleted { .. } => "engine_run_completed",
            Self::EngineRunFailed { .. } => "engine_run_failed",
            Self::SubagentSpawned { .. } => "subagent_spawned",
            Self::SubagentCompleted { .. } => "subagent_completed",
            Self::SubagentTimedOut { .. } => "subagent_timed_out",
            Self::SubagentDepthCapped { .. } => "subagent_depth_capped",
        }
    }

    /// Session key if the event is scoped to one. `None` for global events
    /// (startup, config changes).
    pub fn session_key(&self) -> Option<&str> {
        match self {
            Self::MessageReceived { session_key, .. }
            | Self::MessageSent { session_key, .. }
            | Self::MessageTranscribed { session_key, .. }
            | Self::MessagePreprocessed { session_key, .. }
            | Self::SessionPatch { session_key, .. }
            | Self::AgentBootstrap { session_key, .. }
            | Self::ApprovalRequested { session_key, .. }
            | Self::RateLimitTriggered { session_key, .. } => Some(session_key.as_str()),
            // Subagent events surface the *child's* session_key (when
            // present) — the operator UI's tree visualisation keys on
            // it for the spawn-message expansion. Pre-spawn rejections
            // (DepthCapped) have no child session yet, so report the
            // parent's instead so the parent's row still highlights.
            Self::SubagentSpawned { child_session_key, .. }
            | Self::SubagentCompleted { child_session_key, .. }
            | Self::SubagentTimedOut { child_session_key, .. } => {
                Some(child_session_key.as_str())
            }
            Self::SubagentDepthCapped { parent_session_key, .. } => {
                Some(parent_session_key.as_str())
            }
            Self::GatewayStartup { .. }
            | Self::ConfigChanged { .. }
            | Self::ToolCalled { .. }
            | Self::ApprovalDecided { .. }
            | Self::Telemetry { .. }
            | Self::EngineRunCompleted { .. }
            | Self::EngineRunFailed { .. } => None,
        }
    }
}
