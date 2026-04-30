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
    },
    MessageSent {
        channel: String,
        session_key: String,
        content: String,
        success: bool,
    },
    MessageTranscribed {
        session_key: String,
        transcript: String,
        media_path: String,
        media_type: String,
    },
    MessagePreprocessed {
        session_key: String,
        transcript: String,
        is_group: bool,
        group_id: Option<String>,
    },
    SessionPatch {
        session_key: String,
        patch: serde_json::Value,
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
