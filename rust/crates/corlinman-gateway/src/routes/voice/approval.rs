//! Tool-approval bridge for the `/voice` route.
//!
//! Iter 7 of D4. When the upstream provider yields a
//! [`provider::VoiceEvent::ToolCall`], the voice handler must:
//!
//! 1. **Halt TTS output** to the client (so the user doesn't hear the
//!    assistant continuing while operator approval is pending).
//! 2. **Emit `tool_approval_required`** as a JSON control frame so the
//!    client UI can render the pending banner. The design's "the
//!    client speaks a fixed phrase locally while waiting" is a client
//!    concern — the gateway's job is just to surface the pause point.
//! 3. **File an approval request** via the existing
//!    [`ApprovalGate::check`] (chat surface uses the same gate from
//!    `routes/chat.rs`'s tool-call hot path). The gate writes a
//!    `pending_approvals` row and parks on a oneshot until either:
//!    - an operator decides via `POST /admin/approvals/:id/decide`, or
//!    - the configured timeout elapses
//!    (default `DEFAULT_PROMPT_TIMEOUT = 5 min`).
//! 4. **Resume on the decision**:
//!    - **Approve**: send [`ProviderCommand::ApproveTool { approve:
//!      true }`] upstream and emit an `agent_text` "Approved,
//!      continuing..." so the assistant resumes naturally.
//!    - **Deny / Timeout**: send `ApproveTool { approve: false }` plus
//!      a `ProviderCommand::Interrupt` to flush any in-flight upstream
//!      TTS, then emit an `agent_text` apology so the user knows the
//!      tool was blocked.
//!
//! ## Why a standalone helper?
//!
//! The bridge **composes**. iter 9 wires it into `run_voice_session`
//! by calling [`VoiceApprovalBridge::handle_tool_call`] from inside
//! the provider→client pump task. Keeping the integration logic in a
//! pure async function (no axum / no WebSocket concrete type) lets
//! iter-7 unit tests drive the full approve / deny / timeout matrix
//! against a real `ApprovalGate` without spinning a WebSocket
//! harness. The handler in iter 9 just feeds events through and
//! forwards the resulting [`ApprovalOutcome`] to the appropriate
//! channels.
//!
//! ## Plugin namespacing
//!
//! The gate matches rules by `(plugin, tool)` and the voice provider
//! emits `tool` only — voice tool calls live under a fixed plugin
//! prefix ([`VOICE_TOOL_PLUGIN`]) so an operator can write a single
//! `[[approvals.rules]] plugin = "voice"` rule that gates every tool
//! the realtime API ever surfaces. Tools that already exist as
//! plugins go through the chat-side gate when the agent loop picks
//! them up; the voice path uses the same gate but a dedicated plugin
//! string so audit logs distinguish "operator approved a tool from
//! the voice surface" from "operator approved it from the chat
//! surface".

use std::sync::Arc;

use tokio_util::sync::CancellationToken;
use tracing::{debug, warn};

use crate::middleware::approval::{ApprovalDecision, ApprovalGate};

use super::framing::ServerControl;
use super::provider::ProviderCommand;

/// Plugin string used when filing voice-surface approvals against the
/// gate. Operators write `[[approvals.rules]] plugin = "voice"` to
/// pre-approve / pre-deny every voice tool call.
pub const VOICE_TOOL_PLUGIN: &str = "voice";

/// Default TTS phrase emitted as `agent_text` after an approval is
/// granted. The provider continues from where it paused so the user
/// hears continuation; this text is purely a UI breadcrumb.
pub const APPROVAL_RESUME_TEXT: &str = "Approved, continuing.";

/// Default TTS phrase after a deny / timeout. Per design, the user
/// should know the tool was blocked rather than the call mysteriously
/// going silent. iter 9's bridge follows this with a
/// `ProviderCommand::Interrupt` to make sure no half-emitted TTS
/// leaks through.
pub const APPROVAL_DENIED_TEXT: &str = "Sorry, I'm not allowed to use that tool right now.";

pub const APPROVAL_TIMEOUT_TEXT: &str = "Sorry, I didn't get approval in time to use that tool.";

/// One end of the gate handoff. Iter 9's run_voice_session calls
/// [`VoiceApprovalBridge::handle_tool_call`] once per `VoiceEvent
/// ::ToolCall` and processes the outputs:
///
/// - `Vec<ServerControl>` is forwarded to the client as JSON text frames
///   in order. The first entry is always `ToolApprovalRequired` (so the
///   client UI banner shows up before the wait); later entries are the
///   `AgentText` resume/denial breadcrumb.
/// - `ProviderCommand`s are forwarded to the upstream provider in
///   order. On approve, this is `[ApproveTool { true }]`; on deny /
///   timeout it's `[ApproveTool { false }, Interrupt]` so the upstream
///   TTS buffer is flushed before any apology audio is generated.
#[derive(Debug, Clone)]
pub struct ApprovalOutcome {
    /// Server control frames the gateway must emit to the client, in
    /// order. Always non-empty; the first entry is the
    /// `tool_approval_required` pause point and the rest carry the
    /// resume / denial breadcrumbs.
    pub server_frames: Vec<ServerControl>,
    /// Provider commands the gateway must forward upstream, in order.
    pub provider_commands: Vec<ProviderCommand>,
    /// Final decision the gate returned. Surfaced separately so the
    /// caller can update `voice_sessions.end_reason` if a denial /
    /// cancellation should also terminate the session.
    pub decision: ApprovalDecision,
}

/// Optional handle to the approval gate, scoped to one voice session.
/// `None` means the caller hasn't wired the gate (test harnesses, or a
/// gateway running with `[[approvals.rules]]` empty); in that case the
/// bridge approves every tool call without prompting — same default
/// `NoMatch → Approved` semantics as the chat surface.
#[derive(Clone)]
pub struct VoiceApprovalBridge {
    gate: Option<Arc<ApprovalGate>>,
    /// `session_key` to file under. The chat surface uses the
    /// session's own key; the voice surface mirrors that so
    /// session-scoped whitelists (`allow_session_keys`) work for
    /// trusted operator sessions.
    session_key: String,
}

impl VoiceApprovalBridge {
    /// Construct a bridge that always approves (used when the gateway
    /// has no `ApprovalGate` configured). Tests that exercise the
    /// approve / deny matrix call [`Self::with_gate`] instead.
    pub fn no_gate(session_key: impl Into<String>) -> Self {
        Self {
            gate: None,
            session_key: session_key.into(),
        }
    }

    /// Wire a real gate. The `session_key` is the chat-session key the
    /// voice session reuses so `pending_approvals` rows tie back to
    /// the same session row history the agent loop already writes.
    pub fn with_gate(gate: Arc<ApprovalGate>, session_key: impl Into<String>) -> Self {
        Self {
            gate: Some(gate),
            session_key: session_key.into(),
        }
    }

    /// Drive one tool-call through the approval lifecycle.
    ///
    /// `tool` and `args_json` come from the provider event. `cancel`
    /// is the per-session cancellation token — closing the WebSocket
    /// must cancel the pending wait so the gate can persist a timeout
    /// row without leaking the parked oneshot.
    ///
    /// Returns the [`ApprovalOutcome`] to apply to client + provider
    /// channels; never blocks beyond the gate's own timeout.
    pub async fn handle_tool_call(
        &self,
        approval_id: &str,
        tool: &str,
        args_json: serde_json::Value,
        cancel: CancellationToken,
    ) -> ApprovalOutcome {
        let pause_frame = ServerControl::ToolApprovalRequired {
            approval_id: approval_id.to_string(),
            tool: tool.to_string(),
            args: args_json.clone(),
        };

        let Some(gate) = self.gate.clone() else {
            // No gate wired: approve immediately, but still emit the
            // pause frame so client UX stays consistent across
            // configurations. The agent_text breadcrumb confirms the
            // resume to the user.
            return ApprovalOutcome {
                server_frames: vec![
                    pause_frame,
                    ServerControl::AgentText {
                        text: APPROVAL_RESUME_TEXT.to_string(),
                    },
                ],
                provider_commands: vec![ProviderCommand::ApproveTool {
                    approval_id: approval_id.to_string(),
                    approve: true,
                }],
                decision: ApprovalDecision::Approved,
            };
        };

        // Serialize args to bytes for the gate's `args_json` parameter.
        // The gate stores this verbatim for the admin UI; pretty-print
        // would bloat rows, so use the compact form.
        let args_bytes = serde_json::to_vec(&args_json).unwrap_or_default();
        let result = gate
            .check(
                &self.session_key,
                VOICE_TOOL_PLUGIN,
                tool,
                &args_bytes,
                cancel.clone(),
            )
            .await;

        match result {
            Ok(ApprovalDecision::Approved) => {
                debug!(
                    target: "voice",
                    approval_id, tool, "approval granted; resuming TTS"
                );
                ApprovalOutcome {
                    server_frames: vec![
                        pause_frame,
                        ServerControl::AgentText {
                            text: APPROVAL_RESUME_TEXT.to_string(),
                        },
                    ],
                    provider_commands: vec![ProviderCommand::ApproveTool {
                        approval_id: approval_id.to_string(),
                        approve: true,
                    }],
                    decision: ApprovalDecision::Approved,
                }
            }
            Ok(decision @ ApprovalDecision::Denied(_)) => {
                debug!(
                    target: "voice",
                    approval_id, tool, "approval denied; flushing TTS"
                );
                ApprovalOutcome {
                    server_frames: vec![
                        pause_frame,
                        ServerControl::AgentText {
                            text: APPROVAL_DENIED_TEXT.to_string(),
                        },
                    ],
                    provider_commands: vec![
                        ProviderCommand::ApproveTool {
                            approval_id: approval_id.to_string(),
                            approve: false,
                        },
                        // Flush whatever upstream TTS the provider had
                        // queued for the now-blocked turn so the user
                        // doesn't hear half a sentence.
                        ProviderCommand::Interrupt,
                    ],
                    decision,
                }
            }
            Ok(ApprovalDecision::Timeout) => {
                warn!(
                    target: "voice",
                    approval_id, tool,
                    "approval gate timed out; treating as denial"
                );
                ApprovalOutcome {
                    server_frames: vec![
                        pause_frame,
                        ServerControl::AgentText {
                            text: APPROVAL_TIMEOUT_TEXT.to_string(),
                        },
                    ],
                    provider_commands: vec![
                        ProviderCommand::ApproveTool {
                            approval_id: approval_id.to_string(),
                            approve: false,
                        },
                        ProviderCommand::Interrupt,
                    ],
                    decision: ApprovalDecision::Timeout,
                }
            }
            Err(err) => {
                // Cancellation (client disconnect) or storage failure.
                // We can't ask the user, so the safest action is to
                // deny upstream — the provider must not execute a tool
                // we never confirmed.
                warn!(
                    target: "voice",
                    approval_id, tool, error = %err,
                    "approval gate errored; denying tool call"
                );
                ApprovalOutcome {
                    server_frames: vec![
                        pause_frame,
                        ServerControl::Error {
                            code: "approval_failed".into(),
                            message: format!("approval gate errored: {err}"),
                        },
                    ],
                    provider_commands: vec![
                        ProviderCommand::ApproveTool {
                            approval_id: approval_id.to_string(),
                            approve: false,
                        },
                        ProviderCommand::Interrupt,
                    ],
                    decision: ApprovalDecision::Denied(err.to_string()),
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::middleware::approval::{ApprovalDecision, ApprovalGate};
    use corlinman_core::config::{ApprovalMode, ApprovalRule};
    use corlinman_vector::SqliteStore;
    use std::sync::Arc;
    use std::time::Duration;
    use tempfile::TempDir;

    fn rule(plugin: &str, mode: ApprovalMode) -> ApprovalRule {
        ApprovalRule {
            plugin: plugin.into(),
            tool: None,
            mode,
            allow_session_keys: Vec::new(),
        }
    }

    async fn fresh_gate(
        rules: Vec<ApprovalRule>,
        timeout: Duration,
    ) -> (Arc<ApprovalGate>, TempDir) {
        let tmp = TempDir::new().unwrap();
        let store = SqliteStore::open(&tmp.path().join("kb.sqlite"))
            .await
            .unwrap();
        corlinman_vector::migration::ensure_schema(&store)
            .await
            .unwrap();
        let gate = ApprovalGate::new(rules, Arc::new(store), timeout);
        (Arc::new(gate), tmp)
    }

    #[tokio::test]
    async fn no_gate_approves_synthesises_pause_and_resume() {
        // The "gate not wired" path still emits both the pause frame
        // and a resume breadcrumb so client UX stays consistent.
        let bridge = VoiceApprovalBridge::no_gate("sk-1");
        let outcome = bridge
            .handle_tool_call(
                "ap-1",
                "web_search",
                serde_json::json!({"q": "x"}),
                CancellationToken::new(),
            )
            .await;
        assert_eq!(outcome.decision, ApprovalDecision::Approved);
        assert_eq!(outcome.server_frames.len(), 2);
        assert!(matches!(
            outcome.server_frames[0],
            ServerControl::ToolApprovalRequired { .. }
        ));
        match &outcome.server_frames[0] {
            ServerControl::ToolApprovalRequired {
                approval_id, tool, ..
            } => {
                assert_eq!(approval_id, "ap-1");
                assert_eq!(tool, "web_search");
            }
            _ => unreachable!(),
        }
        assert!(matches!(
            outcome.server_frames[1],
            ServerControl::AgentText { .. }
        ));
        // Approve command goes upstream; no Interrupt on the happy path.
        assert_eq!(outcome.provider_commands.len(), 1);
        match &outcome.provider_commands[0] {
            ProviderCommand::ApproveTool {
                approval_id,
                approve,
            } => {
                assert_eq!(approval_id, "ap-1");
                assert!(*approve);
            }
            other => panic!("expected ApproveTool; got {other:?}"),
        }
    }

    #[tokio::test]
    async fn auto_rule_through_real_gate_approves_without_persistence() {
        // `Auto` mode: gate returns Approved without writing a row.
        // Bridge produces the same shape as no_gate but routes through
        // the real ApprovalGate API.
        let (gate, _tmp) = fresh_gate(
            vec![rule(VOICE_TOOL_PLUGIN, ApprovalMode::Auto)],
            Duration::from_millis(200),
        )
        .await;
        let bridge = VoiceApprovalBridge::with_gate(gate.clone(), "sk-2");
        let outcome = bridge
            .handle_tool_call(
                "ap-2",
                "calc",
                serde_json::json!({"x": 1}),
                CancellationToken::new(),
            )
            .await;
        assert_eq!(outcome.decision, ApprovalDecision::Approved);
        // No row persisted under Auto.
        let rows = gate
            .store_arc_public()
            .list_pending_approvals(true)
            .await
            .unwrap();
        assert!(rows.is_empty(), "Auto must not persist; got {rows:?}");
    }

    #[tokio::test]
    async fn deny_rule_emits_interrupt_and_apology() {
        // Deny rule: bridge must:
        //   - emit pause frame + apology agent_text
        //   - send ApproveTool{false} + Interrupt upstream
        //   - return Denied(_)
        let (gate, _tmp) = fresh_gate(
            vec![rule(VOICE_TOOL_PLUGIN, ApprovalMode::Deny)],
            Duration::from_millis(200),
        )
        .await;
        let bridge = VoiceApprovalBridge::with_gate(gate, "sk-3");
        let outcome = bridge
            .handle_tool_call(
                "ap-3",
                "shell_exec",
                serde_json::json!({}),
                CancellationToken::new(),
            )
            .await;
        assert!(matches!(outcome.decision, ApprovalDecision::Denied(_)));
        assert_eq!(outcome.server_frames.len(), 2);
        assert!(matches!(
            outcome.server_frames[0],
            ServerControl::ToolApprovalRequired { .. }
        ));
        match &outcome.server_frames[1] {
            ServerControl::AgentText { text } => assert_eq!(text, APPROVAL_DENIED_TEXT),
            other => panic!("expected denial AgentText; got {other:?}"),
        }
        // Two upstream commands: deny + interrupt-flush.
        assert_eq!(outcome.provider_commands.len(), 2);
        match &outcome.provider_commands[0] {
            ProviderCommand::ApproveTool { approve, .. } => assert!(!*approve),
            other => panic!("expected ApproveTool{{false}}; got {other:?}"),
        }
        assert_eq!(outcome.provider_commands[1], ProviderCommand::Interrupt);
    }

    #[tokio::test]
    async fn prompt_then_operator_approves_via_resolve() {
        // Realistic flow: rule is Prompt, the operator UI calls
        // gate.resolve(id, Approved). The bridge unblocks and returns
        // an Approved outcome. Mirrors the design's "ping admin UI"
        // path end-to-end.
        let (gate, _tmp) = fresh_gate(
            vec![rule(VOICE_TOOL_PLUGIN, ApprovalMode::Prompt)],
            Duration::from_secs(5),
        )
        .await;
        let bridge = VoiceApprovalBridge::with_gate(gate.clone(), "sk-4");
        let cancel = CancellationToken::new();

        let bridge_clone = bridge.clone();
        let cancel_clone = cancel.clone();
        let handle = tokio::spawn(async move {
            bridge_clone
                .handle_tool_call(
                    "ap-4",
                    "web_search",
                    serde_json::json!({"q":"x"}),
                    cancel_clone,
                )
                .await
        });

        // Wait for the row, then resolve as Approved.
        let id = loop {
            let rows = gate
                .store_arc_public()
                .list_pending_approvals(false)
                .await
                .unwrap();
            if let Some(r) = rows.first() {
                break r.id.clone();
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        };
        gate.resolve(&id, ApprovalDecision::Approved).await.unwrap();

        let outcome = handle.await.unwrap();
        assert_eq!(outcome.decision, ApprovalDecision::Approved);
        // Approve path = pause + resume; one upstream command.
        assert_eq!(outcome.server_frames.len(), 2);
        assert_eq!(outcome.provider_commands.len(), 1);
    }

    #[tokio::test]
    async fn prompt_then_operator_denies_emits_interrupt() {
        let (gate, _tmp) = fresh_gate(
            vec![rule(VOICE_TOOL_PLUGIN, ApprovalMode::Prompt)],
            Duration::from_secs(5),
        )
        .await;
        let bridge = VoiceApprovalBridge::with_gate(gate.clone(), "sk-5");

        let bridge_clone = bridge.clone();
        let handle = tokio::spawn(async move {
            bridge_clone
                .handle_tool_call(
                    "ap-5",
                    "shell_exec",
                    serde_json::json!({}),
                    CancellationToken::new(),
                )
                .await
        });

        let id = loop {
            let rows = gate
                .store_arc_public()
                .list_pending_approvals(false)
                .await
                .unwrap();
            if let Some(r) = rows.first() {
                break r.id.clone();
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        };
        gate.resolve(&id, ApprovalDecision::Denied("not safe".into()))
            .await
            .unwrap();

        let outcome = handle.await.unwrap();
        assert!(matches!(outcome.decision, ApprovalDecision::Denied(_)));
        // Two provider commands: deny ack + flush.
        assert_eq!(outcome.provider_commands.len(), 2);
        assert_eq!(outcome.provider_commands[1], ProviderCommand::Interrupt);
    }

    #[tokio::test]
    async fn prompt_timeout_treated_as_denial() {
        // Gate timeout must surface as Timeout but the bridge translates
        // it to a denial pattern (deny upstream + interrupt) so the user
        // hears a "didn't get approval in time" message instead of
        // silent assistant.
        let (gate, _tmp) = fresh_gate(
            vec![rule(VOICE_TOOL_PLUGIN, ApprovalMode::Prompt)],
            Duration::from_millis(60),
        )
        .await;
        let bridge = VoiceApprovalBridge::with_gate(gate, "sk-6");
        let outcome = bridge
            .handle_tool_call(
                "ap-6",
                "web_search",
                serde_json::json!({"q": "stuck"}),
                CancellationToken::new(),
            )
            .await;
        assert_eq!(outcome.decision, ApprovalDecision::Timeout);
        match &outcome.server_frames[1] {
            ServerControl::AgentText { text } => {
                assert_eq!(text, APPROVAL_TIMEOUT_TEXT);
            }
            other => panic!("expected timeout AgentText; got {other:?}"),
        }
        assert_eq!(outcome.provider_commands.len(), 2);
        assert_eq!(outcome.provider_commands[1], ProviderCommand::Interrupt);
    }

    #[tokio::test]
    async fn cancel_token_aborts_the_wait_and_emits_error() {
        // Client disconnect mid-prompt: cancel the bridge's wait. The
        // gate translates that to a Cancelled error; the bridge must
        // surface a synthetic `Error` server frame and still tell
        // upstream to deny + flush.
        let (gate, _tmp) = fresh_gate(
            vec![rule(VOICE_TOOL_PLUGIN, ApprovalMode::Prompt)],
            Duration::from_secs(5),
        )
        .await;
        let bridge = VoiceApprovalBridge::with_gate(gate, "sk-7");
        let cancel = CancellationToken::new();

        let bridge_clone = bridge.clone();
        let cancel_clone = cancel.clone();
        let handle = tokio::spawn(async move {
            bridge_clone
                .handle_tool_call("ap-7", "web_search", serde_json::json!({}), cancel_clone)
                .await
        });

        // Give the gate a moment to register the pending row, then
        // simulate the client disconnect.
        tokio::time::sleep(Duration::from_millis(30)).await;
        cancel.cancel();

        let outcome = handle.await.unwrap();
        assert!(matches!(outcome.decision, ApprovalDecision::Denied(_)));
        // Error frame surfaces the cause to a debug client; production
        // clients may already be gone but the row in pending_approvals
        // still records a timeout via the gate's normal path.
        assert!(matches!(
            outcome.server_frames[1],
            ServerControl::Error { .. }
        ));
        assert_eq!(outcome.provider_commands[1], ProviderCommand::Interrupt);
    }

    #[tokio::test]
    async fn args_json_round_trips_into_pause_frame() {
        // The args object is forwarded verbatim to the client so the
        // admin/banner UI can render exactly what's pending. Pin this
        // round-trip so a future refactor doesn't accidentally
        // re-stringify it.
        let bridge = VoiceApprovalBridge::no_gate("sk-args");
        let args = serde_json::json!({"q": "blob", "n": 3});
        let outcome = bridge
            .handle_tool_call(
                "ap-args",
                "web_search",
                args.clone(),
                CancellationToken::new(),
            )
            .await;
        match &outcome.server_frames[0] {
            ServerControl::ToolApprovalRequired { args: got, .. } => {
                assert_eq!(got, &args);
            }
            other => panic!("first frame must be ToolApprovalRequired; got {other:?}"),
        }
    }

    #[tokio::test]
    async fn whitelist_session_key_skips_pending_row() {
        // session_key in the rule's `allow_session_keys` short-circuits
        // to Approved without persisting a row — same path the chat
        // surface gets for trusted internal sessions. Voice surface
        // should inherit that behaviour out of the box.
        let allow_rule = ApprovalRule {
            plugin: VOICE_TOOL_PLUGIN.into(),
            tool: None,
            mode: ApprovalMode::Prompt,
            allow_session_keys: vec!["trusted".into()],
        };
        let (gate, _tmp) = fresh_gate(vec![allow_rule], Duration::from_millis(80)).await;
        let bridge = VoiceApprovalBridge::with_gate(gate.clone(), "trusted");
        let outcome = bridge
            .handle_tool_call(
                "ap-w",
                "web_search",
                serde_json::json!({}),
                CancellationToken::new(),
            )
            .await;
        assert_eq!(outcome.decision, ApprovalDecision::Approved);
        let rows = gate
            .store_arc_public()
            .list_pending_approvals(true)
            .await
            .unwrap();
        assert!(rows.is_empty(), "whitelist must skip persistence");
    }
}
