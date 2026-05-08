//! Per-connection MCP session state machine.
//!
//! One WebSocket connection = one [`SessionState`]. The state controls
//! which JSON-RPC methods the dispatcher accepts; transport and adapter
//! wiring happen in iter 4+.
//!
//! State diagram (MCP 2024-11-05 §lifecycle):
//!
//! ```text
//!                  initialize          notifications/initialized
//! [Connected] ───────────────► [Initializing] ───────────────► [Initialized]
//!     │                              │
//!     │ (any non-initialize)         │ (any non-notification request)
//!     ▼                              ▼
//! McpError::SessionNotInitialized    McpError::SessionNotInitialized
//! ```
//!
//! Notes on the `Initializing` middle state:
//! - The spec says the client MUST send `notifications/initialized`
//!   *after* receiving the `initialize` reply, before issuing further
//!   requests. We therefore split `Connected → Initialized` into a
//!   two-step transition so a client that fires `tools/list` before
//!   the initialized notification gets the same -32002 it would for
//!   firing it before `initialize`.
//! - Notifications other than `notifications/initialized` are
//!   tolerated in `Initializing` (e.g. `notifications/cancelled`)
//!   because some clients race those during boot. Only *requests*
//!   (frames with an `id`) are gated.
//!
//! No transport, no async — pure state. The dispatcher in iter 4+
//! holds an `Arc<Mutex<SessionState>>` (or equivalent) per connection.

use crate::error::McpError;
use crate::schema::{InitializeParams, InitializeResult};

/// Method name the client sends to confirm the handshake completed.
pub const INITIALIZED_NOTIFICATION: &str = "notifications/initialized";

/// Method name carrying the handshake parameters.
pub const INITIALIZE_METHOD: &str = "initialize";

/// Lifecycle phases of a single MCP session.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionPhase {
    /// WebSocket upgraded; no `initialize` request seen yet.
    Connected,
    /// Server has replied to `initialize`; awaiting
    /// `notifications/initialized` from the client.
    Initializing,
    /// Handshake complete; `tools/*`, `resources/*`, `prompts/*`
    /// allowed.
    Initialized,
}

impl SessionPhase {
    /// True iff the dispatcher should accept the given method *as a
    /// request* (i.e. with an `id`). Notifications are governed by
    /// [`SessionPhase::accepts_notification`].
    pub fn accepts_request(&self, method: &str) -> bool {
        match self {
            // Pre-initialize: only `initialize` itself is allowed.
            SessionPhase::Connected => method == INITIALIZE_METHOD,
            // Mid-handshake: nothing else is allowed yet — client
            // must send the initialized notification first.
            SessionPhase::Initializing => false,
            // Fully initialized: anything except a duplicate
            // `initialize` (per spec, re-init resets the session, but
            // that's an iter 4+ concern; for now treat it as invalid).
            SessionPhase::Initialized => method != INITIALIZE_METHOD,
        }
    }

    /// True iff the dispatcher should accept the given method *as a
    /// notification* (no `id`). The MCP spec lists
    /// `notifications/cancelled` and `notifications/progress` among
    /// the always-permitted notifications; most others are
    /// implementation-defined. Iter 3 only needs to recognise
    /// `notifications/initialized`; later iters may extend.
    pub fn accepts_notification(&self, method: &str) -> bool {
        // Always-permitted notifications (cancellation, progress)
        // pass through in any phase. The handshake notification is
        // governed by [`SessionState::observe_notification`].
        if method == INITIALIZED_NOTIFICATION {
            // Initialized notification only legal in Initializing.
            return matches!(self, SessionPhase::Initializing);
        }
        // Be liberal with non-handshake notifications; reject only
        // when no session is up yet (Connected). Otherwise some
        // clients race `notifications/cancelled` past boot.
        !matches!(self, SessionPhase::Connected)
    }
}

/// Session state owned by the dispatcher for a single WS connection.
#[derive(Debug, Clone)]
pub struct SessionState {
    phase: SessionPhase,
    /// Protocol version the client requested in `initialize`. Stored
    /// so adapters can branch on it later (e.g. C2 might raise to
    /// 2025-06-18). `None` until the handshake reaches `Initializing`.
    client_protocol_version: Option<String>,
    /// The `clientInfo` the client supplied — kept for logging /
    /// telemetry. Iter 3 doesn't dispatch on it.
    client_name: Option<String>,
    client_version: Option<String>,
}

impl Default for SessionState {
    fn default() -> Self {
        Self::new()
    }
}

impl SessionState {
    /// Build a fresh session in the [`SessionPhase::Connected`]
    /// phase.
    pub fn new() -> Self {
        Self {
            phase: SessionPhase::Connected,
            client_protocol_version: None,
            client_name: None,
            client_version: None,
        }
    }

    /// Current phase. Cheap to copy.
    pub fn phase(&self) -> SessionPhase {
        self.phase
    }

    pub fn client_protocol_version(&self) -> Option<&str> {
        self.client_protocol_version.as_deref()
    }

    pub fn client_name(&self) -> Option<&str> {
        self.client_name.as_deref()
    }

    pub fn client_version(&self) -> Option<&str> {
        self.client_version.as_deref()
    }

    /// Apply the `initialize` request. Captures the client metadata
    /// and advances `Connected → Initializing`. Returns
    /// `McpError::InvalidRequest` if called outside `Connected`
    /// (re-init mid-session is an iter 4+ concern).
    ///
    /// The matching [`InitializeResult`] is *not* constructed here —
    /// that lives in the dispatcher, which knows the configured
    /// server capabilities. This method only mutates state.
    pub fn observe_initialize(
        &mut self,
        params: &InitializeParams,
    ) -> Result<(), McpError> {
        if self.phase != SessionPhase::Connected {
            return Err(McpError::InvalidRequest(format!(
                "duplicate `initialize`; session already in {:?}",
                self.phase
            )));
        }
        self.client_protocol_version = Some(params.protocol_version.clone());
        self.client_name = Some(params.client_info.name.clone());
        self.client_version = Some(params.client_info.version.clone());
        self.phase = SessionPhase::Initializing;
        Ok(())
    }

    /// Apply the client's `notifications/initialized`. Advances
    /// `Initializing → Initialized`. Returns
    /// `SessionNotInitialized` if called before `initialize`.
    pub fn observe_initialized_notification(
        &mut self,
    ) -> Result<(), McpError> {
        match self.phase {
            SessionPhase::Connected => Err(McpError::SessionNotInitialized),
            SessionPhase::Initializing => {
                self.phase = SessionPhase::Initialized;
                Ok(())
            }
            // Already initialized — duplicate notifications are a
            // benign no-op per spec.
            SessionPhase::Initialized => Ok(()),
        }
    }

    /// Pre-flight a request. Returns the right [`McpError`] variant
    /// for the dispatcher to lift into a JSON-RPC error frame, or
    /// `Ok(())` if the method is admissible in this phase.
    ///
    /// The dispatcher still has to do its own per-method routing on
    /// success — this only enforces the lifecycle gate.
    pub fn check_request_allowed(&self, method: &str) -> Result<(), McpError> {
        if self.phase.accepts_request(method) {
            return Ok(());
        }
        // Two distinct failure modes:
        //   - Connected & non-initialize → SessionNotInitialized
        //   - Initializing & anything    → SessionNotInitialized
        //   - Initialized & duplicate `initialize` → InvalidRequest
        if self.phase == SessionPhase::Initialized && method == INITIALIZE_METHOD
        {
            Err(McpError::InvalidRequest(
                "session already initialized; duplicate `initialize` not supported"
                    .into(),
            ))
        } else {
            Err(McpError::SessionNotInitialized)
        }
    }

    /// Pre-flight a notification frame.
    pub fn check_notification_allowed(
        &self,
        method: &str,
    ) -> Result<(), McpError> {
        if self.phase.accepts_notification(method) {
            Ok(())
        } else {
            // Pre-initialize, only the initialized notification could
            // legitimately be sent — and the spec orders that *after*
            // the initialize reply. So any notification in `Connected`
            // is a session-not-initialized.
            Err(McpError::SessionNotInitialized)
        }
    }
}

/// Convenience constructor used by the dispatcher in iter 4+ when it
/// has finished crafting the [`InitializeResult`] reply. Kept here so
/// the state machine and the canonical reply shape live next to each
/// other.
pub fn initialize_reply(
    server_capabilities: crate::schema::ServerCapabilities,
    server_name: impl Into<String>,
    server_version: impl Into<String>,
) -> InitializeResult {
    InitializeResult {
        protocol_version: crate::schema::MCP_PROTOCOL_VERSION.to_string(),
        capabilities: server_capabilities,
        server_info: crate::schema::Implementation {
            name: server_name.into(),
            version: server_version.into(),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::{ClientCapabilities, Implementation, ServerCapabilities};

    fn sample_initialize_params() -> InitializeParams {
        InitializeParams {
            protocol_version: "2024-11-05".into(),
            capabilities: ClientCapabilities::default(),
            client_info: Implementation {
                name: "claude-desktop".into(),
                version: "0.7.4".into(),
            },
        }
    }

    #[test]
    fn fresh_session_starts_in_connected_phase() {
        let s = SessionState::new();
        assert_eq!(s.phase(), SessionPhase::Connected);
        assert!(s.client_protocol_version().is_none());
        assert!(s.client_name().is_none());
    }

    #[test]
    fn happy_path_connected_to_initialized_via_two_steps() {
        let mut s = SessionState::new();
        // Step 1: initialize
        s.observe_initialize(&sample_initialize_params())
            .expect("initialize must succeed from Connected");
        assert_eq!(s.phase(), SessionPhase::Initializing);
        assert_eq!(s.client_protocol_version(), Some("2024-11-05"));
        assert_eq!(s.client_name(), Some("claude-desktop"));
        assert_eq!(s.client_version(), Some("0.7.4"));

        // Step 2: initialized notification
        s.observe_initialized_notification()
            .expect("initialized notification must succeed from Initializing");
        assert_eq!(s.phase(), SessionPhase::Initialized);
    }

    #[test]
    fn tools_list_before_initialize_returns_session_not_initialized() {
        let s = SessionState::new();
        let err = s
            .check_request_allowed("tools/list")
            .expect_err("must reject non-initialize from Connected");
        assert!(
            matches!(err, McpError::SessionNotInitialized),
            "expected SessionNotInitialized, got {err:?}"
        );
        // Mapping to JSON-RPC code -32002 confirmed in error tests;
        // here we just verify the variant.
    }

    #[test]
    fn tools_list_during_initializing_phase_still_rejected() {
        let mut s = SessionState::new();
        s.observe_initialize(&sample_initialize_params()).unwrap();
        // Client cheats and fires tools/list before sending the
        // initialized notification.
        let err = s
            .check_request_allowed("tools/list")
            .expect_err("Initializing phase must reject requests");
        assert!(matches!(err, McpError::SessionNotInitialized));
    }

    #[test]
    fn initialize_request_in_initialized_phase_returns_invalid_request() {
        let mut s = SessionState::new();
        s.observe_initialize(&sample_initialize_params()).unwrap();
        s.observe_initialized_notification().unwrap();
        // Client tries to re-initialize.
        let err = s
            .check_request_allowed("initialize")
            .expect_err("re-init in Initialized must fail");
        match err {
            McpError::InvalidRequest(msg) => {
                assert!(
                    msg.contains("already initialized"),
                    "message must explain the rejection, got {msg:?}"
                );
            }
            other => panic!("expected InvalidRequest, got {other:?}"),
        }
    }

    #[test]
    fn duplicate_initialize_during_initializing_returns_invalid_request() {
        let mut s = SessionState::new();
        s.observe_initialize(&sample_initialize_params()).unwrap();
        // Second initialize from same client (broken client).
        let err = s
            .observe_initialize(&sample_initialize_params())
            .expect_err("second initialize must fail");
        assert!(matches!(err, McpError::InvalidRequest(_)));
    }

    #[test]
    fn initialized_notification_before_initialize_rejected() {
        let mut s = SessionState::new();
        let err = s
            .observe_initialized_notification()
            .expect_err("initialized notification before initialize must fail");
        assert!(matches!(err, McpError::SessionNotInitialized));
        // State must not have advanced.
        assert_eq!(s.phase(), SessionPhase::Connected);
    }

    #[test]
    fn duplicate_initialized_notification_is_idempotent_no_op() {
        let mut s = SessionState::new();
        s.observe_initialize(&sample_initialize_params()).unwrap();
        s.observe_initialized_notification().unwrap();
        // Spec: duplicates are benign.
        s.observe_initialized_notification()
            .expect("duplicate initialized notification must be a no-op");
        assert_eq!(s.phase(), SessionPhase::Initialized);
    }

    #[test]
    fn check_notification_allowed_gates_initialized_to_initializing_only() {
        let mut s = SessionState::new();
        // Connected: rejected.
        assert!(s
            .check_notification_allowed(INITIALIZED_NOTIFICATION)
            .is_err());
        // Initializing: allowed.
        s.observe_initialize(&sample_initialize_params()).unwrap();
        assert!(s
            .check_notification_allowed(INITIALIZED_NOTIFICATION)
            .is_ok());
        // Initialized: not "allowed" by the gate (no longer the
        // expected next event), but `observe_initialized_notification`
        // still treats it as an idempotent no-op. The gate is a
        // dispatch-time pre-check; idempotency is the apply-time
        // contract.
        s.observe_initialized_notification().unwrap();
        let allowed = s.check_notification_allowed(INITIALIZED_NOTIFICATION);
        assert!(
            allowed.is_err(),
            "post-handshake the gate filters duplicates; observe_* still tolerates them"
        );
    }

    #[test]
    fn cancel_notification_allowed_after_handshake_starts() {
        let mut s = SessionState::new();
        // Connected: rejected.
        assert!(s
            .check_notification_allowed("notifications/cancelled")
            .is_err());
        s.observe_initialize(&sample_initialize_params()).unwrap();
        // Initializing: tolerated (some clients race cancels past
        // boot).
        assert!(s
            .check_notification_allowed("notifications/cancelled")
            .is_ok());
        s.observe_initialized_notification().unwrap();
        assert!(s
            .check_notification_allowed("notifications/cancelled")
            .is_ok());
    }

    #[test]
    fn initialize_reply_pins_protocol_version_and_server_info() {
        let result = initialize_reply(
            ServerCapabilities::default(),
            "corlinman",
            "0.1.0",
        );
        assert_eq!(result.protocol_version, "2024-11-05");
        assert_eq!(result.server_info.name, "corlinman");
        assert_eq!(result.server_info.version, "0.1.0");
    }
}
