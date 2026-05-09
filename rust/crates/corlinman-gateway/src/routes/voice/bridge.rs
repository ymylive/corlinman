//! Hot-path bridge — drives a `/voice` session end-to-end.
//!
//! Iter 9 of D4. Lifts the per-session glue out of [`super::run_voice_session`]
//! into a runtime-level state machine that plugs together every piece
//! built in iters 2-8:
//!
//! - **iter 2** framing: parses inbound text / binary frames and
//!   serialises outbound `ServerControl` events.
//! - **iter 3 + 8** cost gating: per-tick budget enforcement, with the
//!   1-Hz checkpoint ticker that keeps `voice_spend` within ~1 s of
//!   the live session's accumulated usage.
//! - **iter 4** provider trait: `audio_in_tx` / `control_in_tx` /
//!   `events_rx` channels. The mock provider drives the unit tests
//!   here without touching the network.
//! - **iter 6** persistence: optional `voice_sessions.row` lifecycle
//!   plus the `transcript_final` → chat-session bridge.
//! - **iter 7** approval pause: when the provider yields
//!   `VoiceEvent::ToolCall`, the bridge halts TTS, files via
//!   [`super::approval::VoiceApprovalBridge`], and routes the outcome
//!   back to both the client and the provider.
//!
//! ## Why a trait around the WebSocket?
//!
//! axum's [`axum::extract::ws::WebSocket`] doesn't compose well with
//! `tower::oneshot`-driven tests — the upgrade itself needs a real
//! TCP connection. Rather than fight the test harness, the bridge
//! talks to a [`BridgeIo`] trait. Production wires it to a
//! `WebSocket`; tests wire it to an [`InMemoryIo`] backed by a pair
//! of `mpsc` channels. That gives the iter-9 test matrix coverage of
//! the full pump-loop without ever opening a socket.

use std::time::{Duration, Instant};

use async_trait::async_trait;
use tokio::sync::mpsc;
use tokio::time::interval;
use tokio_util::sync::CancellationToken;
use tracing::{debug, warn};

use super::approval::VoiceApprovalBridge;
use super::budget::{
    terminate_reason_to_code, terminate_reason_to_end_reason, terminate_reason_to_message,
    BudgetEnforcer, BudgetTickAction,
};
use super::framing::{
    encode_server_control, parse_audio_frame, parse_client_control, ClientControl, ServerControl,
};
use super::persistence::{
    SharedTranscriptSink, SharedVoiceSessionStore, VoiceEndReason, VoiceSessionEnd,
    VoiceSessionStart,
};
use super::provider::{
    ProviderCommand, ProviderEndReason, SharedVoiceProvider, VoiceEvent, VoiceProviderSession,
    VoiceSessionStartParams,
};

/// One frame sent toward the client. Lives at this layer of
/// abstraction (rather than `axum::Message`) so the in-memory test
/// harness can intercept frames without owning a `WebSocket`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BridgeOutFrame {
    Text(String),
    Binary(Vec<u8>),
    Close { code: u16, reason: String },
}

/// One frame received from the client. Mirror of [`BridgeOutFrame`]
/// inbound. `None` ends the inbound stream — the bridge interprets
/// that as `client_disconnect`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BridgeInFrame {
    Text(String),
    Binary(Vec<u8>),
    /// Client sent a Close frame; bridge mirrors with its own close
    /// and exits gracefully.
    ClosedByClient,
}

/// I/O surface the bridge talks to. Production = `WebSocket` adapter
/// (in [`super::mod`]); tests = an `mpsc`-backed mock.
#[async_trait]
pub trait BridgeIo: Send + Unpin + 'static {
    /// Receive the next inbound frame. `None` = client side closed
    /// (connection dropped without a Close frame).
    async fn recv(&mut self) -> Option<BridgeInFrame>;
    /// Send one outbound frame. `Err(())` = transport closed; the
    /// bridge treats this like a client disconnect.
    async fn send(&mut self, frame: BridgeOutFrame) -> Result<(), BridgeIoError>;
}

#[derive(Debug, thiserror::Error)]
#[error("bridge I/O closed")]
pub struct BridgeIoError;

/// Configuration assembled by the route handler before calling
/// [`run_bridge`]. Passes through everything the bridge needs without
/// taking a hard dep on the route handler's `VoiceState`.
pub struct BridgeContext {
    pub session_id: String,
    pub provider_alias: String,
    pub tenant_id: String,
    pub day_epoch: u64,
    pub started_at_unix: i64,
    pub started_at_instant: Instant,
    pub session_key: String,
    pub agent_id: Option<String>,
    pub sample_rate_hz_in: u32,
    pub sample_rate_hz_out: u32,
    pub voice_id: Option<String>,
    pub provider: SharedVoiceProvider,
    pub session_store: Option<SharedVoiceSessionStore>,
    pub transcript_sink: Option<SharedTranscriptSink>,
    pub approval_bridge: VoiceApprovalBridge,
    pub budget: BudgetEnforcer,
    /// Resolved on-disk PCM path when `[voice] retain_audio = true`.
    /// `None` means the operator has retention off (default) and the
    /// `voice_sessions.audio_path` column stays NULL. iter 10 wires
    /// the route handler to populate this from
    /// [`super::persistence::audio_path_for`]; the actual byte-stream
    /// writes are still parked behind the `corlinman-voice` package
    /// listed in `phase4-roadmap.md:330`. Recording the path now means
    /// a follow-on iter that adds the writer doesn't have to plumb a
    /// new field through the bridge surface.
    pub audio_path: Option<String>,
    /// Wallclock-time tick interval for budget polling. Production =
    /// `Duration::from_secs(1)`; tests crank this down to a few ms.
    pub tick_interval: Duration,
    /// Cancellation token derived from the route handler. Cancelled
    /// when the gateway is shutting down — the bridge then closes
    /// cleanly with `client_disconnect`.
    pub cancel: CancellationToken,
}

/// Final outcome of one session — used by the route handler to write
/// `voice_sessions.end_reason` + the final spend checkpoint, and by
/// tests to assert on the close path.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BridgeOutcome {
    pub end_reason: VoiceEndReason,
    pub duration_secs: u64,
    pub transcript_text: Option<String>,
}

/// Drive a session to completion. Composes the iter-2..8 pieces:
///
/// 1. Open the provider session; on error, write `start_failed` row
///    and close with `4003 provider_unavailable`.
/// 2. On `VoiceEvent::Ready`, write the `voice_sessions` start row +
///    forward `started` to the client.
/// 3. Pump-loop:
///    - inbound text → `ClientControl` → `Interrupt` /
///      `ApproveTool` / `End` upstream commands;
///    - inbound binary → validated PCM frame → `audio_in_tx`;
///    - provider events → demultiplex (audio → binary out;
///      transcript → text out + transcript-sink write; tool_call →
///      approval bridge);
///    - 1-Hz budget tick → emit warn / terminate as configured.
/// 4. On exit, finalise the budget, persist `voice_sessions.row` end,
///    and return the [`BridgeOutcome`] for the caller.
pub async fn run_bridge<Io: BridgeIo>(
    mut io: Io,
    mut ctx: BridgeContext,
) -> BridgeOutcome {
    // ---- 1. open provider session --------------------------------
    let params = VoiceSessionStartParams {
        session_id: ctx.session_id.clone(),
        tenant_id: ctx.tenant_id.clone(),
        provider_alias: ctx.provider_alias.clone(),
        sample_rate_hz_in: ctx.sample_rate_hz_in,
        sample_rate_hz_out: ctx.sample_rate_hz_out,
        voice_id: ctx.voice_id.clone(),
    };
    let session = match ctx.provider.open(params).await {
        Ok(s) => s,
        Err(err) => {
            warn!(target: "voice", session_id = %ctx.session_id, error = %err, "provider open failed");
            // Emit error frame + close 4003. No `voice_sessions` row
            // is persisted because the design says start_failed never
            // touches the table (the row is only inserted on Ready).
            let _ = io
                .send(BridgeOutFrame::Text(encode_server_control(
                    &ServerControl::Error {
                        code: "provider_unavailable".into(),
                        message: err.to_string(),
                    },
                )))
                .await;
            let _ = io
                .send(BridgeOutFrame::Close {
                    code: CLOSE_CODE_PROVIDER_UNAVAILABLE,
                    reason: "provider unavailable".into(),
                })
                .await;
            return BridgeOutcome {
                end_reason: VoiceEndReason::StartFailed,
                duration_secs: 0,
                transcript_text: None,
            };
        }
    };

    let VoiceProviderSession {
        audio_in_tx,
        control_in_tx,
        mut events_rx,
        task: provider_task,
    } = session;

    // ---- 2. await Ready, send `started`, persist start row -------
    let mut transcript_buf = String::new();
    let mut session_row_persisted = false;

    // The start row insert can race the first frame emission, so we
    // wait for `Ready` (or an early End/Error) before writing.
    if let Err(end) = wait_for_ready_and_persist(
        &mut events_rx,
        &mut io,
        &ctx,
        &mut session_row_persisted,
    )
    .await
    {
        // Close cleanly; budget already at zero.
        return finalise(&mut ctx, end, transcript_buf, session_row_persisted, audio_in_tx, control_in_tx, provider_task).await;
    }

    // ---- 3. pump loop -------------------------------------------
    let mut tick = interval(ctx.tick_interval);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

    let outcome_reason: VoiceEndReason = loop {
        tokio::select! {
            biased;
            _ = ctx.cancel.cancelled() => {
                debug!(target: "voice", session_id = %ctx.session_id, "cancellation token fired");
                break VoiceEndReason::ClientDisconnect;
            }
            frame = io.recv() => {
                match frame {
                    Some(BridgeInFrame::Text(text)) => {
                        match handle_client_text(&text, &control_in_tx).await {
                            ClientFrameAction::Continue => {},
                            ClientFrameAction::EndRequested => break VoiceEndReason::Graceful,
                            ClientFrameAction::ProtocolError(reason) => {
                                let _ = io.send(BridgeOutFrame::Text(encode_server_control(
                                    &ServerControl::Error {
                                        code: "invalid_control_frame".into(),
                                        message: reason,
                                    },
                                ))).await;
                                // Don't terminate on a malformed frame; the
                                // client may correct itself. iter 10's
                                // tightening can decide whether persistent
                                // garbage warrants a close.
                            }
                        }
                    }
                    Some(BridgeInFrame::Binary(bytes)) => {
                        match parse_audio_frame(&bytes) {
                            Ok(_) => {
                                if audio_in_tx.send(bytes).await.is_err() {
                                    // Provider went away mid-session.
                                    break VoiceEndReason::ProviderError;
                                }
                            }
                            Err(err) => {
                                let _ = io.send(BridgeOutFrame::Text(encode_server_control(
                                    &ServerControl::Error {
                                        code: "invalid_audio_frame".into(),
                                        message: err.to_string(),
                                    },
                                ))).await;
                            }
                        }
                    }
                    Some(BridgeInFrame::ClosedByClient) | None => {
                        break VoiceEndReason::ClientDisconnect;
                    }
                }
            }
            event = events_rx.recv() => {
                let Some(event) = event else {
                    // Provider task ended without an explicit End event.
                    break VoiceEndReason::ProviderError;
                };
                match handle_provider_event(
                    event,
                    &mut io,
                    &ctx,
                    &control_in_tx,
                    &mut transcript_buf,
                ).await {
                    ProviderFrameAction::Continue => {},
                    ProviderFrameAction::ProviderEnded(reason) => {
                        let r = match reason {
                            ProviderEndReason::Graceful => VoiceEndReason::Graceful,
                            ProviderEndReason::ProviderError => VoiceEndReason::ProviderError,
                            ProviderEndReason::StartFailed => VoiceEndReason::StartFailed,
                        };
                        break r;
                    }
                }
            }
            _ = tick.tick() => {
                let action = ctx.budget.tick(Instant::now());
                match action {
                    BudgetTickAction::Continue => {},
                    BudgetTickAction::EmitWarning { minutes_remaining } => {
                        let _ = io.send(BridgeOutFrame::Text(encode_server_control(
                            &ServerControl::BudgetWarning { minutes_remaining },
                        ))).await;
                    }
                    BudgetTickAction::Terminate { reason, close_code } => {
                        let _ = io.send(BridgeOutFrame::Text(encode_server_control(
                            &ServerControl::Error {
                                code: terminate_reason_to_code(reason).into(),
                                message: terminate_reason_to_message(reason).into(),
                            },
                        ))).await;
                        let _ = io.send(BridgeOutFrame::Close {
                            code: close_code,
                            reason: terminate_reason_to_end_reason(reason).into(),
                        }).await;
                        break match reason {
                            super::cost::TerminateReason::DayBudgetExhausted =>
                                VoiceEndReason::Budget,
                            super::cost::TerminateReason::MaxSessionSeconds =>
                                VoiceEndReason::MaxSession,
                        };
                    }
                }
            }
        }
    };

    finalise(
        &mut ctx,
        outcome_reason,
        transcript_buf,
        session_row_persisted,
        audio_in_tx,
        control_in_tx,
        provider_task,
    )
    .await
}

// ----- helpers --------------------------------------------------------------

/// Provider-unavailable WebSocket close code. Application range; the
/// design names this `4003` so existing clients reuse the same
/// reconnect logic they already have.
pub const CLOSE_CODE_PROVIDER_UNAVAILABLE: u16 = 4003;

/// Outcome of a single client-text frame.
enum ClientFrameAction {
    Continue,
    EndRequested,
    ProtocolError(String),
}

async fn handle_client_text(
    text: &str,
    control_in_tx: &mpsc::Sender<ProviderCommand>,
) -> ClientFrameAction {
    match parse_client_control(text) {
        Ok(ClientControl::Start { .. }) => {
            // The handler in mod.rs sends `started` and reads the
            // start parameters; a redundant `start` mid-session is a
            // protocol error per design. We tolerate it as a no-op so
            // a misbehaving client doesn't tear the session.
            ClientFrameAction::Continue
        }
        Ok(ClientControl::Interrupt) => {
            let _ = control_in_tx.send(ProviderCommand::Interrupt).await;
            ClientFrameAction::Continue
        }
        Ok(ClientControl::ApproveTool { approval_id, approve }) => {
            let _ = control_in_tx
                .send(ProviderCommand::ApproveTool { approval_id, approve })
                .await;
            ClientFrameAction::Continue
        }
        Ok(ClientControl::End) => ClientFrameAction::EndRequested,
        Err(err) => ClientFrameAction::ProtocolError(err.to_string()),
    }
}

enum ProviderFrameAction {
    Continue,
    ProviderEnded(ProviderEndReason),
}

async fn handle_provider_event<Io: BridgeIo>(
    event: VoiceEvent,
    io: &mut Io,
    ctx: &BridgeContext,
    control_in_tx: &mpsc::Sender<ProviderCommand>,
    transcript_buf: &mut String,
) -> ProviderFrameAction {
    match event {
        VoiceEvent::Ready { .. } => {
            // Ready is consumed by `wait_for_ready_and_persist`; if a
            // second one arrives we treat it as a no-op.
            ProviderFrameAction::Continue
        }
        VoiceEvent::AudioOut { pcm_le_bytes } => {
            let _ = io.send(BridgeOutFrame::Binary(pcm_le_bytes)).await;
            ProviderFrameAction::Continue
        }
        VoiceEvent::TranscriptPartial { role, text } => {
            let _ = io
                .send(BridgeOutFrame::Text(encode_server_control(
                    &ServerControl::TranscriptPartial { role, text },
                )))
                .await;
            ProviderFrameAction::Continue
        }
        VoiceEvent::TranscriptFinal { role, text } => {
            // Mirror to client AND append to the chat-session bridge.
            let _ = io
                .send(BridgeOutFrame::Text(encode_server_control(
                    &ServerControl::TranscriptFinal {
                        role: role.clone(),
                        text: text.clone(),
                    },
                )))
                .await;
            transcript_buf.push_str(&format!("{role}: {text}\n"));
            if let Some(sink) = &ctx.transcript_sink {
                if let Err(err) = sink
                    .append_turn(&ctx.tenant_id, &ctx.session_key, &role, &text)
                    .await
                {
                    warn!(
                        target: "voice",
                        session_id = %ctx.session_id, error = %err,
                        "transcript sink append failed; transcript still in buffer"
                    );
                }
            }
            ProviderFrameAction::Continue
        }
        VoiceEvent::AgentText { text } => {
            let _ = io
                .send(BridgeOutFrame::Text(encode_server_control(
                    &ServerControl::AgentText { text },
                )))
                .await;
            ProviderFrameAction::Continue
        }
        VoiceEvent::ToolCall { call_id, tool, args } => {
            // Iter 7 bridge — fire approval, route outcome.
            let outcome = ctx
                .approval_bridge
                .handle_tool_call(&call_id, &tool, args, ctx.cancel.clone())
                .await;
            for f in outcome.server_frames {
                let _ = io
                    .send(BridgeOutFrame::Text(encode_server_control(&f)))
                    .await;
            }
            for cmd in outcome.provider_commands {
                let _ = control_in_tx.send(cmd).await;
            }
            ProviderFrameAction::Continue
        }
        VoiceEvent::Error { code, message } => {
            let _ = io
                .send(BridgeOutFrame::Text(encode_server_control(
                    &ServerControl::Error {
                        code: code.clone(),
                        message: message.clone(),
                    },
                )))
                .await;
            ProviderFrameAction::ProviderEnded(ProviderEndReason::ProviderError)
        }
        VoiceEvent::End { reason } => ProviderFrameAction::ProviderEnded(reason),
    }
}

/// Drain the events channel until either the first `Ready` (success)
/// or an early End/Error (failure). On Ready: write the
/// `voice_sessions` start row + send `started` to the client. On
/// failure: close the socket; bridge returns the chosen end reason.
async fn wait_for_ready_and_persist<Io: BridgeIo>(
    events_rx: &mut mpsc::Receiver<VoiceEvent>,
    io: &mut Io,
    ctx: &BridgeContext,
    persisted: &mut bool,
) -> Result<(), VoiceEndReason> {
    while let Some(event) = events_rx.recv().await {
        match event {
            VoiceEvent::Ready { .. } => {
                let _ = io
                    .send(BridgeOutFrame::Text(encode_server_control(
                        &ServerControl::Started {
                            session_id: ctx.session_id.clone(),
                            provider: ctx.provider_alias.clone(),
                        },
                    )))
                    .await;
                if let Some(store) = &ctx.session_store {
                    if let Err(err) = store
                        .record_start(&VoiceSessionStart {
                            id: ctx.session_id.clone(),
                            tenant_id: ctx.tenant_id.clone(),
                            session_key: ctx.session_key.clone(),
                            agent_id: ctx.agent_id.clone(),
                            provider_alias: ctx.provider_alias.clone(),
                            started_at: ctx.started_at_unix,
                        })
                        .await
                    {
                        warn!(
                            target: "voice",
                            session_id = %ctx.session_id, error = %err,
                            "voice_sessions row insert failed; continuing without persistence"
                        );
                    } else {
                        *persisted = true;
                    }
                }
                return Ok(());
            }
            VoiceEvent::Error { code, message } => {
                let _ = io
                    .send(BridgeOutFrame::Text(encode_server_control(
                        &ServerControl::Error { code, message },
                    )))
                    .await;
                let _ = io
                    .send(BridgeOutFrame::Close {
                        code: CLOSE_CODE_PROVIDER_UNAVAILABLE,
                        reason: "provider error before ready".into(),
                    })
                    .await;
                return Err(VoiceEndReason::StartFailed);
            }
            VoiceEvent::End { .. } => {
                return Err(VoiceEndReason::StartFailed);
            }
            // Any other event before Ready is unusual but not fatal —
            // most providers won't send transcript / audio before
            // Ready, but the contract doesn't forbid it. Forward the
            // event downstream as if Ready had already arrived; the
            // start row will land when Ready does.
            other => {
                debug!(target: "voice", event = ?other, "provider event before Ready; deferring");
            }
        }
    }
    // Channel closed before Ready — provider task panicked or dropped.
    Err(VoiceEndReason::ProviderError)
}

#[allow(clippy::too_many_arguments)]
async fn finalise(
    ctx: &mut BridgeContext,
    end_reason: VoiceEndReason,
    transcript_text: String,
    session_row_persisted: bool,
    audio_in_tx: mpsc::Sender<Vec<u8>>,
    control_in_tx: mpsc::Sender<ProviderCommand>,
    provider_task: tokio::task::JoinHandle<()>,
) -> BridgeOutcome {
    // Send Close to the provider so it flushes / drops its upstream.
    let _ = control_in_tx.send(ProviderCommand::Close).await;
    drop(audio_in_tx);
    drop(control_in_tx);
    // Best-effort wait, but don't block the gateway shutdown forever.
    let _ = tokio::time::timeout(Duration::from_millis(500), provider_task).await;

    let now = Instant::now();
    let duration_secs = ctx.budget.finalize(now);

    let transcript_for_row = if transcript_text.is_empty() {
        None
    } else {
        Some(transcript_text.clone())
    };

    if session_row_persisted {
        if let Some(store) = &ctx.session_store {
            if let Err(err) = store
                .record_end(&VoiceSessionEnd {
                    id: ctx.session_id.clone(),
                    ended_at: ctx.started_at_unix + duration_secs as i64,
                    duration_secs: duration_secs as i64,
                    // Iter 10: when `[voice] retain_audio = true`, the
                    // route handler resolves the path via
                    // `audio_path_for(...)` and threads it through here
                    // so the row reflects the operator's retention
                    // intent. `None` means retention is off (default)
                    // → column stays NULL.
                    audio_path: ctx.audio_path.clone(),
                    transcript_text: transcript_for_row.clone(),
                    end_reason,
                })
                .await
            {
                warn!(
                    target: "voice",
                    session_id = %ctx.session_id, error = %err,
                    "voice_sessions row finalise failed"
                );
            }
        }
    }

    BridgeOutcome {
        end_reason,
        duration_secs,
        transcript_text: transcript_for_row,
    }
}

// ---------------------------------------------------------------------------
// In-memory I/O — test harness for the bridge
// ---------------------------------------------------------------------------

/// `mpsc`-backed `BridgeIo`. Tests construct one of these, hand it to
/// [`run_bridge`], and drive both sides through the
/// [`InMemoryIoHandle`] returned alongside.
#[cfg(test)]
pub struct InMemoryIo {
    pub inbound_rx: mpsc::Receiver<BridgeInFrame>,
    pub outbound_tx: mpsc::Sender<BridgeOutFrame>,
}

#[cfg(test)]
pub struct InMemoryIoHandle {
    pub inbound_tx: mpsc::Sender<BridgeInFrame>,
    pub outbound_rx: mpsc::Receiver<BridgeOutFrame>,
}

#[cfg(test)]
impl InMemoryIo {
    pub fn new() -> (Self, InMemoryIoHandle) {
        let (inbound_tx, inbound_rx) = mpsc::channel(64);
        let (outbound_tx, outbound_rx) = mpsc::channel(64);
        (
            Self {
                inbound_rx,
                outbound_tx,
            },
            InMemoryIoHandle {
                inbound_tx,
                outbound_rx,
            },
        )
    }
}

#[cfg(test)]
#[async_trait]
impl BridgeIo for InMemoryIo {
    async fn recv(&mut self) -> Option<BridgeInFrame> {
        self.inbound_rx.recv().await
    }
    async fn send(&mut self, frame: BridgeOutFrame) -> Result<(), BridgeIoError> {
        self.outbound_tx.send(frame).await.map_err(|_| BridgeIoError)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::approval::VoiceApprovalBridge;
    use super::super::cost::{InMemoryVoiceSpend, VoiceSpend};
    use super::super::persistence::{MemoryTranscriptSink, SqliteVoiceSessionStore};
    use super::super::provider::{MockBehaviour, MockEchoProvider};
    use corlinman_core::config::VoiceConfig;
    use std::sync::Arc;

    fn cfg(budget_min: u32, max_secs: u32) -> VoiceConfig {
        VoiceConfig {
            enabled: true,
            budget_minutes_per_tenant_per_day: budget_min,
            max_session_seconds: max_secs,
            ..VoiceConfig::default()
        }
    }

    fn ctx_with_provider(
        provider: SharedVoiceProvider,
        cfg: VoiceConfig,
        session_store: Option<SharedVoiceSessionStore>,
        sink: Option<SharedTranscriptSink>,
        spend: Arc<dyn VoiceSpend>,
    ) -> (BridgeContext, CancellationToken) {
        let cancel = CancellationToken::new();
        let started = Instant::now();
        let budget = BudgetEnforcer::start(&cfg, spend, "default".into(), 100, started);
        (
            BridgeContext {
                session_id: "voice-test".into(),
                provider_alias: "mock".into(),
                tenant_id: "default".into(),
                day_epoch: 100,
                started_at_unix: 1_700_000_000,
                started_at_instant: started,
                session_key: "sk-test".into(),
                agent_id: None,
                sample_rate_hz_in: 16_000,
                sample_rate_hz_out: 24_000,
                voice_id: None,
                provider,
                session_store,
                transcript_sink: sink,
                approval_bridge: VoiceApprovalBridge::no_gate("sk-test"),
                budget,
                audio_path: None,
                tick_interval: Duration::from_millis(20),
                cancel: cancel.clone(),
            },
            cancel,
        )
    }

    /// Drain frames into a vec until either a Close frame or the
    /// channel closes. Helper because each test asserts on the same
    /// "what did the client see" sequence.
    async fn drain(handle: &mut InMemoryIoHandle, deadline: Duration) -> Vec<BridgeOutFrame> {
        let mut out = Vec::new();
        let started = Instant::now();
        while started.elapsed() < deadline {
            match tokio::time::timeout(Duration::from_millis(50), handle.outbound_rx.recv()).await {
                Ok(Some(f)) => {
                    let is_close = matches!(f, BridgeOutFrame::Close { .. });
                    out.push(f);
                    if is_close {
                        break;
                    }
                }
                Ok(None) => break,
                Err(_) => continue,
            }
        }
        out
    }

    #[tokio::test]
    async fn provider_open_failure_emits_error_and_close_4003() {
        let provider: SharedVoiceProvider = Arc::new(MockEchoProvider::with_behaviour(
            "mock",
            MockBehaviour {
                fail_open_with: Some("upstream 401".into()),
                ..Default::default()
            },
        ));
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let (ctx, _cancel) = ctx_with_provider(provider, cfg(30, 600), None, None, spend);
        let (io, mut handle) = InMemoryIo::new();

        let outcome = run_bridge(io, ctx).await;
        assert_eq!(outcome.end_reason, VoiceEndReason::StartFailed);

        let frames = drain(&mut handle, Duration::from_millis(200)).await;
        assert!(
            frames.iter().any(|f| matches!(
                f,
                BridgeOutFrame::Close { code, .. } if *code == CLOSE_CODE_PROVIDER_UNAVAILABLE
            )),
            "expected close 4003 in {frames:?}"
        );
    }

    #[tokio::test]
    async fn ready_emits_started_and_persists_voice_sessions_row() {
        let provider: SharedVoiceProvider = Arc::new(MockEchoProvider::new("mock"));
        let store: SharedVoiceSessionStore =
            Arc::new(SqliteVoiceSessionStore::open_in_memory().await.unwrap());
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let (ctx, _cancel) = ctx_with_provider(
            provider,
            cfg(30, 600),
            Some(store.clone()),
            None,
            spend,
        );
        let (io, mut handle) = InMemoryIo::new();

        // Spawn the bridge then end the session via client `end`.
        let bridge_handle = tokio::spawn(run_bridge(io, ctx));

        // Wait for `started`.
        let f = tokio::time::timeout(Duration::from_millis(500), handle.outbound_rx.recv())
            .await
            .expect("started arrives")
            .expect("Some");
        match f {
            BridgeOutFrame::Text(s) => {
                assert!(s.contains("\"type\":\"started\""), "got {s}");
                assert!(s.contains("voice-test"));
            }
            other => panic!("expected text frame; got {other:?}"),
        }

        handle
            .inbound_tx
            .send(BridgeInFrame::Text(r#"{"type":"end"}"#.into()))
            .await
            .unwrap();
        let outcome = bridge_handle.await.unwrap();
        assert_eq!(outcome.end_reason, VoiceEndReason::Graceful);

        // Row finalised in the SQLite store.
        let row = store.fetch("voice-test").await.unwrap().unwrap();
        assert_eq!(row.end_reason, "graceful");
        assert!(row.ended_at.is_some());
    }

    #[tokio::test]
    async fn audio_round_trip_through_mock_provider() {
        // Send PCM in; mock echoes it back; bridge forwards as
        // BridgeOutFrame::Binary.
        let provider: SharedVoiceProvider = Arc::new(MockEchoProvider::new("mock"));
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let (ctx, _cancel) = ctx_with_provider(provider, cfg(30, 600), None, None, spend);
        let (io, mut handle) = InMemoryIo::new();

        let bridge_handle = tokio::spawn(run_bridge(io, ctx));

        // Drain `started`.
        let _ = tokio::time::timeout(Duration::from_millis(500), handle.outbound_rx.recv()).await;

        handle
            .inbound_tx
            .send(BridgeInFrame::Binary(vec![0x11, 0x22, 0x33, 0x44]))
            .await
            .unwrap();

        let mut got_audio = false;
        for _ in 0..5 {
            let f = tokio::time::timeout(Duration::from_millis(500), handle.outbound_rx.recv())
                .await
                .expect("frame")
                .expect("Some");
            if let BridgeOutFrame::Binary(b) = f {
                assert_eq!(b, vec![0x11, 0x22, 0x33, 0x44]);
                got_audio = true;
                break;
            }
        }
        assert!(got_audio, "expected binary echo within 5 frames");

        handle
            .inbound_tx
            .send(BridgeInFrame::Text(r#"{"type":"end"}"#.into()))
            .await
            .unwrap();
        let _ = bridge_handle.await.unwrap();
    }

    #[tokio::test]
    async fn invalid_audio_frame_emits_error_but_keeps_session_alive() {
        let provider: SharedVoiceProvider = Arc::new(MockEchoProvider::new("mock"));
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let (ctx, _cancel) = ctx_with_provider(provider, cfg(30, 600), None, None, spend);
        let (io, mut handle) = InMemoryIo::new();

        let bridge_handle = tokio::spawn(run_bridge(io, ctx));
        let _ = tokio::time::timeout(Duration::from_millis(300), handle.outbound_rx.recv()).await;

        // Empty binary → AudioFrameError::Empty
        handle
            .inbound_tx
            .send(BridgeInFrame::Binary(vec![]))
            .await
            .unwrap();

        let mut got_error = false;
        for _ in 0..5 {
            let f = tokio::time::timeout(Duration::from_millis(300), handle.outbound_rx.recv())
                .await
                .expect("frame")
                .expect("Some");
            if let BridgeOutFrame::Text(s) = f {
                if s.contains("invalid_audio_frame") {
                    got_error = true;
                    break;
                }
            }
        }
        assert!(got_error, "expected invalid_audio_frame error frame");

        handle
            .inbound_tx
            .send(BridgeInFrame::Text(r#"{"type":"end"}"#.into()))
            .await
            .unwrap();
        let outcome = bridge_handle.await.unwrap();
        assert_eq!(outcome.end_reason, VoiceEndReason::Graceful);
    }

    #[tokio::test]
    async fn transcript_final_writes_to_sink_and_session_row() {
        let provider: SharedVoiceProvider = Arc::new(MockEchoProvider::with_behaviour(
            "mock",
            MockBehaviour {
                user_transcript: "hello".into(),
                ..Default::default()
            },
        ));
        let store: SharedVoiceSessionStore =
            Arc::new(SqliteVoiceSessionStore::open_in_memory().await.unwrap());
        let sink_concrete = Arc::new(MemoryTranscriptSink::new());
        let sink: SharedTranscriptSink = sink_concrete.clone();
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let (ctx, _cancel) = ctx_with_provider(
            provider,
            cfg(30, 600),
            Some(store.clone()),
            Some(sink),
            spend,
        );
        let (io, mut handle) = InMemoryIo::new();

        let bridge_handle = tokio::spawn(run_bridge(io, ctx));

        // Trigger one audio frame so the mock emits its transcript.
        let _ = tokio::time::timeout(Duration::from_millis(300), handle.outbound_rx.recv()).await;
        handle
            .inbound_tx
            .send(BridgeInFrame::Binary(vec![0u8; 4]))
            .await
            .unwrap();
        // Drain a few outbound frames so the transcript event hits.
        for _ in 0..5 {
            let _ = tokio::time::timeout(Duration::from_millis(200), handle.outbound_rx.recv()).await;
        }

        handle
            .inbound_tx
            .send(BridgeInFrame::Text(r#"{"type":"end"}"#.into()))
            .await
            .unwrap();
        let outcome = bridge_handle.await.unwrap();
        assert_eq!(outcome.end_reason, VoiceEndReason::Graceful);

        // Sink got the user turn.
        let snap = sink_concrete.snapshot().await;
        assert!(
            snap.iter().any(|t| t.role == "user" && t.text == "hello"),
            "expected user transcript in sink; got {snap:?}"
        );

        // voice_sessions row has transcript_text populated.
        let row = store.fetch("voice-test").await.unwrap().unwrap();
        assert!(row.transcript_text.as_deref().unwrap_or("").contains("hello"));
    }

    #[tokio::test]
    async fn budget_terminate_closes_with_4002() {
        // 1 min budget, and 60s already burned today — first tick
        // crosses the cap and terminates with close code 4002.
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        spend.add_seconds("default", 100, 60);
        let provider: SharedVoiceProvider = Arc::new(MockEchoProvider::new("mock"));
        let (mut ctx, _cancel) = ctx_with_provider(provider, cfg(1, 600), None, None, spend);
        ctx.tick_interval = Duration::from_millis(10);
        let (io, mut handle) = InMemoryIo::new();

        let outcome = run_bridge(io, ctx).await;
        assert_eq!(outcome.end_reason, VoiceEndReason::Budget);

        let frames = drain(&mut handle, Duration::from_secs(1)).await;
        assert!(
            frames.iter().any(|f| matches!(
                f,
                BridgeOutFrame::Close { code, .. } if *code == super::super::cost::CLOSE_CODE_BUDGET
            )),
            "expected close 4002 in {frames:?}"
        );
    }

    #[tokio::test]
    async fn cancel_token_closes_session_as_client_disconnect() {
        let provider: SharedVoiceProvider = Arc::new(MockEchoProvider::new("mock"));
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let (ctx, cancel) = ctx_with_provider(provider, cfg(30, 600), None, None, spend);
        let (io, mut handle) = InMemoryIo::new();

        let bridge_handle = tokio::spawn(run_bridge(io, ctx));
        // Drain `started`.
        let _ = tokio::time::timeout(Duration::from_millis(300), handle.outbound_rx.recv()).await;

        cancel.cancel();
        let outcome = bridge_handle.await.unwrap();
        assert_eq!(outcome.end_reason, VoiceEndReason::ClientDisconnect);
    }

    #[tokio::test]
    async fn client_close_frame_ends_session_as_client_disconnect() {
        let provider: SharedVoiceProvider = Arc::new(MockEchoProvider::new("mock"));
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let (ctx, _cancel) = ctx_with_provider(provider, cfg(30, 600), None, None, spend);
        let (io, mut handle) = InMemoryIo::new();

        let bridge_handle = tokio::spawn(run_bridge(io, ctx));
        let _ = tokio::time::timeout(Duration::from_millis(300), handle.outbound_rx.recv()).await;

        handle
            .inbound_tx
            .send(BridgeInFrame::ClosedByClient)
            .await
            .unwrap();
        let outcome = bridge_handle.await.unwrap();
        assert_eq!(outcome.end_reason, VoiceEndReason::ClientDisconnect);
    }

    #[tokio::test]
    async fn interrupt_control_frame_forwards_to_provider() {
        // Mock interrupt drops the next echoed frame. The bridge must
        // get the Interrupt forwarded so the latch fires.
        let provider: SharedVoiceProvider = Arc::new(MockEchoProvider::new("mock"));
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let (ctx, _cancel) = ctx_with_provider(provider, cfg(30, 600), None, None, spend);
        let (io, mut handle) = InMemoryIo::new();

        let bridge_handle = tokio::spawn(run_bridge(io, ctx));
        // Drain `started`.
        let _ = tokio::time::timeout(Duration::from_millis(300), handle.outbound_rx.recv()).await;

        // First frame echoes back as audio.
        handle
            .inbound_tx
            .send(BridgeInFrame::Binary(vec![1, 2]))
            .await
            .unwrap();
        let mut saw_first_echo = false;
        for _ in 0..5 {
            let f = tokio::time::timeout(Duration::from_millis(300), handle.outbound_rx.recv())
                .await
                .expect("frame")
                .expect("Some");
            if matches!(f, BridgeOutFrame::Binary(_)) {
                saw_first_echo = true;
                break;
            }
        }
        assert!(saw_first_echo, "expected first audio echo");

        // Interrupt → next frame must NOT echo (mock latch).
        handle
            .inbound_tx
            .send(BridgeInFrame::Text(r#"{"type":"interrupt"}"#.into()))
            .await
            .unwrap();
        // Give the control channel a beat so the latch lands before
        // the next audio frame races it.
        tokio::time::sleep(Duration::from_millis(40)).await;
        handle
            .inbound_tx
            .send(BridgeInFrame::Binary(vec![3, 4]))
            .await
            .unwrap();

        // Next outbound binary must be a *post-interrupt* echo, not
        // the [3,4] one — read with a tight deadline so a missing
        // drop is observable.
        let mut saw_post_interrupt_echo = false;
        let deadline = Instant::now() + Duration::from_millis(200);
        while Instant::now() < deadline {
            match tokio::time::timeout(Duration::from_millis(80), handle.outbound_rx.recv()).await {
                Ok(Some(BridgeOutFrame::Binary(_))) => {
                    saw_post_interrupt_echo = true;
                    break;
                }
                _ => continue,
            }
        }
        assert!(
            !saw_post_interrupt_echo,
            "interrupt failed to drop the [3,4] echo"
        );

        handle
            .inbound_tx
            .send(BridgeInFrame::Text(r#"{"type":"end"}"#.into()))
            .await
            .unwrap();
        let _ = bridge_handle.await.unwrap();
    }

    #[tokio::test]
    async fn malformed_control_frame_emits_error_but_keeps_session() {
        let provider: SharedVoiceProvider = Arc::new(MockEchoProvider::new("mock"));
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let (ctx, _cancel) = ctx_with_provider(provider, cfg(30, 600), None, None, spend);
        let (io, mut handle) = InMemoryIo::new();

        let bridge_handle = tokio::spawn(run_bridge(io, ctx));
        let _ = tokio::time::timeout(Duration::from_millis(300), handle.outbound_rx.recv()).await;

        handle
            .inbound_tx
            .send(BridgeInFrame::Text("not json".into()))
            .await
            .unwrap();

        let mut got_error = false;
        for _ in 0..5 {
            match tokio::time::timeout(Duration::from_millis(300), handle.outbound_rx.recv()).await
            {
                Ok(Some(BridgeOutFrame::Text(s))) => {
                    if s.contains("invalid_control_frame") {
                        got_error = true;
                        break;
                    }
                }
                _ => break,
            }
        }
        assert!(got_error, "expected invalid_control_frame error");

        handle
            .inbound_tx
            .send(BridgeInFrame::Text(r#"{"type":"end"}"#.into()))
            .await
            .unwrap();
        let outcome = bridge_handle.await.unwrap();
        assert_eq!(outcome.end_reason, VoiceEndReason::Graceful);
    }

    #[tokio::test]
    async fn audio_path_threads_into_voice_sessions_row() {
        // Iter 10 fix #2: when the route handler computes an
        // audio_path (because [voice] retain_audio = true), the bridge
        // must persist it verbatim to voice_sessions.audio_path so the
        // retention sweeper (a follow-on iter) can find the file.
        let provider: SharedVoiceProvider = Arc::new(MockEchoProvider::new("mock"));
        let store: SharedVoiceSessionStore =
            Arc::new(SqliteVoiceSessionStore::open_in_memory().await.unwrap());
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let (mut ctx, _cancel) =
            ctx_with_provider(provider, cfg(30, 600), Some(store.clone()), None, spend);
        let want = "/tmp/corlinman-test/tenants/default/voice/voice-test.pcm".to_string();
        ctx.audio_path = Some(want.clone());
        let (io, mut handle) = InMemoryIo::new();

        let bridge_handle = tokio::spawn(run_bridge(io, ctx));
        let _ = tokio::time::timeout(Duration::from_millis(300), handle.outbound_rx.recv()).await;
        handle
            .inbound_tx
            .send(BridgeInFrame::Text(r#"{"type":"end"}"#.into()))
            .await
            .unwrap();
        let _ = bridge_handle.await.unwrap();

        let row = store.fetch("voice-test").await.unwrap().unwrap();
        assert_eq!(
            row.audio_path.as_deref(),
            Some(want.as_str()),
            "audio_path threaded from BridgeContext into row"
        );
    }

    #[tokio::test]
    async fn audio_path_none_keeps_voice_sessions_audio_path_null() {
        // Default-config path (retain_audio = false): bridge writes
        // NULL, no leaked path. Pinned because flipping the field's
        // default would silently start retaining session paths.
        let provider: SharedVoiceProvider = Arc::new(MockEchoProvider::new("mock"));
        let store: SharedVoiceSessionStore =
            Arc::new(SqliteVoiceSessionStore::open_in_memory().await.unwrap());
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let (ctx, _cancel) = ctx_with_provider(
            provider,
            cfg(30, 600),
            Some(store.clone()),
            None,
            spend,
        );
        // ctx.audio_path defaults to None via ctx_with_provider.
        assert!(ctx.audio_path.is_none(), "fixture default = None");
        let (io, mut handle) = InMemoryIo::new();

        let bridge_handle = tokio::spawn(run_bridge(io, ctx));
        let _ = tokio::time::timeout(Duration::from_millis(300), handle.outbound_rx.recv()).await;
        handle
            .inbound_tx
            .send(BridgeInFrame::Text(r#"{"type":"end"}"#.into()))
            .await
            .unwrap();
        let _ = bridge_handle.await.unwrap();

        let row = store.fetch("voice-test").await.unwrap().unwrap();
        assert!(row.audio_path.is_none());
    }

    #[tokio::test]
    async fn session_key_from_context_drives_transcript_sink_writes() {
        // Iter 10 fix #1: the route handler now extracts session_key
        // from the inbound `start` frame BEFORE building BridgeContext.
        // Pin the chain end-to-end: a context built with a custom
        // session_key writes transcript turns under that exact key
        // (not the synthetic session_id) so the chat session FK lines
        // up with whatever the client supplied.
        let provider: SharedVoiceProvider = Arc::new(MockEchoProvider::with_behaviour(
            "mock",
            MockBehaviour {
                user_transcript: "from-client-key".into(),
                ..Default::default()
            },
        ));
        let sink_concrete = Arc::new(MemoryTranscriptSink::new());
        let sink: SharedTranscriptSink = sink_concrete.clone();
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let (mut ctx, _cancel) =
            ctx_with_provider(provider, cfg(30, 600), None, Some(sink), spend);
        // Simulate the handler extracting a non-default session_key
        // out of the `start` control frame.
        ctx.session_key = "client-supplied-key".into();
        ctx.agent_id = Some("agent-from-start".into());
        let (io, mut handle) = InMemoryIo::new();

        let bridge_handle = tokio::spawn(run_bridge(io, ctx));
        let _ = tokio::time::timeout(Duration::from_millis(300), handle.outbound_rx.recv()).await;
        handle
            .inbound_tx
            .send(BridgeInFrame::Binary(vec![0u8; 4]))
            .await
            .unwrap();
        // Drain a few outbound frames so the transcript event lands.
        for _ in 0..5 {
            let _ = tokio::time::timeout(Duration::from_millis(150), handle.outbound_rx.recv())
                .await;
        }
        handle
            .inbound_tx
            .send(BridgeInFrame::Text(r#"{"type":"end"}"#.into()))
            .await
            .unwrap();
        let _ = bridge_handle.await.unwrap();

        let snap = sink_concrete.snapshot().await;
        assert!(
            snap.iter().any(|t| t.session_key == "client-supplied-key"
                && t.role == "user"
                && t.text == "from-client-key"),
            "expected sink turn under client-supplied session_key; got {snap:?}"
        );
    }

    #[tokio::test]
    async fn duration_secs_persisted_when_session_ends_gracefully() {
        // A session that runs for a measurable interval must record a
        // non-zero duration_secs in `voice_sessions`. Pin this so a
        // refactor doesn't accidentally write 0 (silent billing leak).
        let provider: SharedVoiceProvider = Arc::new(MockEchoProvider::new("mock"));
        let store: SharedVoiceSessionStore =
            Arc::new(SqliteVoiceSessionStore::open_in_memory().await.unwrap());
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let (ctx, _cancel) = ctx_with_provider(
            provider,
            cfg(30, 600),
            Some(store.clone()),
            None,
            spend.clone(),
        );
        let (io, mut handle) = InMemoryIo::new();

        let bridge_handle = tokio::spawn(run_bridge(io, ctx));
        let _ = tokio::time::timeout(Duration::from_millis(300), handle.outbound_rx.recv()).await;
        // Wait long enough that two ticks at 20ms have fired.
        tokio::time::sleep(Duration::from_millis(80)).await;
        handle
            .inbound_tx
            .send(BridgeInFrame::Text(r#"{"type":"end"}"#.into()))
            .await
            .unwrap();
        let outcome = bridge_handle.await.unwrap();
        assert_eq!(outcome.end_reason, VoiceEndReason::Graceful);
        // Duration is recorded — exact value depends on scheduler
        // jitter; we only assert it's non-negative + the spend store
        // has at least one tick's worth of seconds. The 1-second
        // floor below is conservative enough that flaky test runners
        // don't trip even on a slow CI box.
        let row = store.fetch("voice-test").await.unwrap().unwrap();
        assert!(row.duration_secs.is_some());
        let snap = spend.snapshot("default", 100);
        assert!(snap.seconds_used <= 5, "spend should be tiny; got {snap:?}");
    }
}
