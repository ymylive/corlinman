//! Voice provider adapter trait + mock implementation.
//!
//! Iter 4 of D4. Lifts the upstream-WebSocket integration behind a
//! pluggable trait so iter 5 (real OpenAI Realtime) and the mock used
//! by tests both implement the same shape. The handler in [`super`]
//! never knows which provider is on the other end of the channels —
//! the adapter is the single waist point.
//!
//! ## Shape
//!
//! Each `/voice` session spawns one [`VoiceProviderSession`] via
//! [`VoiceProvider::open`]. The session is a tri-channel object:
//!
//! - `audio_in_tx` — gateway pumps client PCM-16 frames in
//! - `control_in_tx` — gateway forwards client control frames
//!   (`interrupt`, `approve_tool`, `end`) and gateway-side commands
//!   (e.g. `abort` mid-session)
//! - `events_rx` — provider drains [`VoiceEvent`] back; the gateway
//!   demultiplexes into binary TTS frames + JSON `ServerControl`
//!
//! The trait deliberately speaks in semantic events (`AudioOut`,
//! `TranscriptPartial`, `ToolCall`, …) rather than provider-shaped
//! JSON. iter 5's OpenAI adapter does the envelope translation
//! (`response.audio.delta` → `AudioOut`); the gateway's pump tasks
//! stay provider-agnostic.
//!
//! ## Why a trait, not a generic
//!
//! The provider is selected at runtime by `[voice] provider_alias`
//! lookup against the providers registry. The handler can't be
//! monomorphised over a concrete provider type without re-introducing
//! the alias resolution at compile time. `Arc<dyn VoiceProvider>` keeps
//! the route handler shape stable while iter 5+ adds providers.

use std::sync::Arc;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;

/// Default channel depth for the audio/event pumps. 64 frames at
/// ~20 ms each = ~1.3 s of headroom — enough to absorb a brief stall
/// without dropping but small enough that a stuck consumer surfaces
/// quickly via backpressure.
pub const DEFAULT_PROVIDER_CHANNEL_CAPACITY: usize = 64;

/// Inbound from gateway → provider. Small enum, deliberately not
/// `serde`-tagged: it never crosses a wire, only an `mpsc` channel.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProviderCommand {
    /// Client requested barge-in. Provider should flush any pending
    /// TTS so the next utterance starts cleanly.
    Interrupt,
    /// Operator approved (true) / denied (false) a pending tool call.
    /// Iter 7 wires this to the actual approval gate; iter 4 only
    /// defines the shape.
    ApproveTool { approval_id: String, approve: bool },
    /// Gateway-initiated terminal close. Provider should flush state
    /// and stop emitting events.
    Close,
}

/// Provider → gateway events. Each variant maps to one or more
/// outbound WebSocket frames in the route handler.
///
/// Kept separate from the wire-shaped [`super::framing::ServerControl`]
/// because the provider may emit events the wire never carries (e.g.
/// raw `usage` deltas the gateway aggregates locally).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum VoiceEvent {
    /// Provider is up and ready for audio input. Sent before any
    /// `AudioOut` so the gateway can ack `start` to the client.
    Ready { provider_session_id: String },
    /// Outbound TTS PCM-16 chunk, little-endian, 24 kHz mono unless
    /// the provider negotiated otherwise. The gateway forwards as a
    /// binary WebSocket message verbatim.
    AudioOut { pcm_le_bytes: Vec<u8> },
    /// Interim ASR — may change.
    TranscriptPartial { role: String, text: String },
    /// Final committed turn. The gateway writes this to the chat
    /// session table (iter 6) so the agent loop sees voice turns.
    TranscriptFinal { role: String, text: String },
    /// Assistant text mirroring TTS audio. Some providers emit text
    /// before audio finishes streaming; we forward it immediately.
    AgentText { text: String },
    /// Provider yielded a tool call. The gateway translates to the
    /// existing `ServerFrame::ToolCall` shape and routes through the
    /// agent loop / approval gate.
    ToolCall {
        call_id: String,
        tool: String,
        args: serde_json::Value,
    },
    /// Provider emitted an error; gateway closes the session.
    Error { code: String, message: String },
    /// Provider closed cleanly. Gateway flushes transcript + closes
    /// the client socket.
    End { reason: ProviderEndReason },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProviderEndReason {
    /// Session ended in response to a `Close` command.
    Graceful,
    /// Provider terminated unexpectedly (network drop, upstream bug).
    ProviderError,
    /// Provider declined to start (auth, quota, etc.). Sent before
    /// any `Ready`.
    StartFailed,
}

/// Configuration handed to the adapter at session start. Pulled from
/// `[voice]` config at the route handler so the trait stays decoupled
/// from `corlinman-core`'s config shape.
#[derive(Debug, Clone)]
pub struct VoiceSessionStartParams {
    /// `voice-<uuid>` minted by the gateway; carried in logs so a
    /// provider-side incident report ties back to a gateway session.
    pub session_id: String,
    /// Tenant slug (for per-tenant API key routing in the real
    /// adapter).
    pub tenant_id: String,
    /// Resolved by `[voice] provider_alias`. Adapters are free to
    /// ignore this if they only ever serve one provider.
    pub provider_alias: String,
    /// Sample rate the client is producing. Adapters that resample
    /// upstream get this verbatim.
    pub sample_rate_hz_in: u32,
    /// Sample rate the gateway emits to the client. Adapters that
    /// can negotiate the upstream TTS rate use this as the target.
    pub sample_rate_hz_out: u32,
    /// Optional `agent_card.voice_id` passthrough (design Q3).
    pub voice_id: Option<String>,
}

/// Live channels for one open provider session.
///
/// The handler keeps the senders / receiver and drives the three pump
/// tasks; the provider implementation owns whatever state it needs to
/// translate to/from its upstream.
#[derive(Debug)]
pub struct VoiceProviderSession {
    /// Send PCM-16 audio frames in. Closing this signals end-of-input.
    pub audio_in_tx: mpsc::Sender<Vec<u8>>,
    /// Send control commands in. Closing this is the same as sending
    /// [`ProviderCommand::Close`] but allows graceful drain.
    pub control_in_tx: mpsc::Sender<ProviderCommand>,
    /// Drain provider events. The stream ends when the provider task
    /// exits (clean close, error, or aborted).
    pub events_rx: mpsc::Receiver<VoiceEvent>,
    /// Handle to the provider's background task. The handler awaits
    /// this on session shutdown to surface any panic.
    pub task: JoinHandle<()>,
}

/// The provider-adapter trait. Implementations live in:
///
/// - [`MockEchoProvider`] — round-trip echo for unit tests; ships in
///   the test cfg only.
/// - iter 5: `OpenAIRealtimeProvider` — a `wss://api.openai.com`
///   client behind `OPENAI_API_KEY`.
#[async_trait]
pub trait VoiceProvider: Send + Sync {
    /// Adapter identifier for logs / metrics. Must match the alias
    /// the route handler resolved from `[voice] provider_alias`.
    fn alias(&self) -> &str;

    /// Open one session. Adapters spawn whatever background tasks
    /// they need (one upstream WebSocket, a translation task, …) and
    /// wire them into the returned channel ends.
    ///
    /// Returns an `Err` only if the session can't even start (auth
    /// failure, immediately-rejected upstream). A mid-session
    /// failure is reported via [`VoiceEvent::Error`] / `End`.
    async fn open(
        &self,
        params: VoiceSessionStartParams,
    ) -> Result<VoiceProviderSession, ProviderOpenError>;
}

/// Reasons a provider declines to even start a session. Maps to the
/// gateway's pre-upgrade error responses where possible — once the
/// WebSocket is upgraded, mid-session failures use [`VoiceEvent::Error`]
/// instead.
#[derive(Debug, thiserror::Error)]
pub enum ProviderOpenError {
    /// Adapter alias did not resolve to a configured provider.
    #[error("provider alias not configured: {alias}")]
    UnknownAlias { alias: String },
    /// Required credentials are missing (no API key, no token).
    #[error("provider credentials missing: {detail}")]
    MissingCredentials { detail: String },
    /// Upstream refused at handshake (HTTP 401/429/etc.).
    #[error("provider upstream rejected: {detail}")]
    Upstream { detail: String },
    /// Catch-all for I/O / runtime failures.
    #[error("provider open failed: {detail}")]
    Other { detail: String },
}

// ---------------------------------------------------------------------------
// Mock provider — test-only echo
// ---------------------------------------------------------------------------

/// Test double for the provider. Echoes audio back as TTS frames and
/// emits a synthetic transcript pair so handler-level integration tests
/// can drive a full session without network.
///
/// **Test-only**: gated under `#[cfg(test)]` so production builds never
/// link the mock. The route handler in iter 4 doesn't pick the mock
/// itself — tests construct one explicitly and inject it through the
/// state seam.
#[cfg(test)]
pub struct MockEchoProvider {
    alias: String,
    /// Preconfigured behaviour knobs the test wants to exercise. A
    /// mutex (not `RwLock`) because mutation only happens on `open`,
    /// which isn't on the hot path.
    behaviour: std::sync::Mutex<MockBehaviour>,
}

#[cfg(test)]
#[derive(Debug, Clone, Default)]
pub struct MockBehaviour {
    /// If set, `open` returns this error instead of a live session.
    pub fail_open_with: Option<String>,
    /// Static transcript the mock emits as `TranscriptFinal{role:user}`
    /// after the first audio frame. Empty = skip the event.
    pub user_transcript: String,
    /// Static text + audio the mock emits as the agent reply.
    pub agent_text: String,
    /// Synthetic TTS PCM bytes the mock echoes back per inbound audio
    /// frame. If empty, the mock echoes the inbound bytes verbatim.
    pub tts_pcm_per_frame: Option<Vec<u8>>,
    /// Optional artificial delay between inbound audio and the echoed
    /// `AudioOut` event — pins barge-in tests' timing without sleeping
    /// in the test itself.
    pub frame_delay: Option<std::time::Duration>,
}

#[cfg(test)]
impl MockEchoProvider {
    pub fn new(alias: impl Into<String>) -> Self {
        Self {
            alias: alias.into(),
            behaviour: std::sync::Mutex::new(MockBehaviour::default()),
        }
    }

    pub fn with_behaviour(alias: impl Into<String>, b: MockBehaviour) -> Self {
        Self {
            alias: alias.into(),
            behaviour: std::sync::Mutex::new(b),
        }
    }
}

#[cfg(test)]
#[async_trait]
impl VoiceProvider for MockEchoProvider {
    fn alias(&self) -> &str {
        &self.alias
    }

    async fn open(
        &self,
        params: VoiceSessionStartParams,
    ) -> Result<VoiceProviderSession, ProviderOpenError> {
        let behaviour = self
            .behaviour
            .lock()
            .expect("mock behaviour mutex poisoned")
            .clone();

        if let Some(detail) = behaviour.fail_open_with.clone() {
            return Err(ProviderOpenError::Upstream { detail });
        }

        let (audio_in_tx, mut audio_in_rx) =
            mpsc::channel::<Vec<u8>>(DEFAULT_PROVIDER_CHANNEL_CAPACITY);
        let (control_in_tx, mut control_in_rx) =
            mpsc::channel::<ProviderCommand>(DEFAULT_PROVIDER_CHANNEL_CAPACITY);
        let (events_tx, events_rx) =
            mpsc::channel::<VoiceEvent>(DEFAULT_PROVIDER_CHANNEL_CAPACITY);

        let session_id = params.session_id.clone();

        // Background task — emits `Ready` then translates audio in to
        // audio out (echo). Tracks "first frame" so the user transcript
        // fires once.
        let task = tokio::spawn(async move {
            // Emit ready immediately so the route handler can ack
            // `start` to the client.
            if events_tx
                .send(VoiceEvent::Ready {
                    provider_session_id: format!("mock-{}", session_id),
                })
                .await
                .is_err()
            {
                return;
            }

            let mut first_frame_seen = false;
            let mut interrupted = false;

            loop {
                tokio::select! {
                    biased;
                    cmd = control_in_rx.recv() => {
                        match cmd {
                            Some(ProviderCommand::Close) | None => {
                                let _ = events_tx.send(VoiceEvent::End {
                                    reason: ProviderEndReason::Graceful,
                                }).await;
                                return;
                            }
                            Some(ProviderCommand::Interrupt) => {
                                interrupted = true;
                                // Drain any backlog of audio_in so the
                                // next audio_in event is post-interrupt.
                                while audio_in_rx.try_recv().is_ok() {}
                            }
                            Some(ProviderCommand::ApproveTool { approval_id, approve }) => {
                                // Mock translates an approval into an
                                // `AgentText` echo so tests can pin the
                                // resume-after-approval shape.
                                let text = if approve {
                                    format!("approved:{approval_id}")
                                } else {
                                    format!("denied:{approval_id}")
                                };
                                if events_tx.send(VoiceEvent::AgentText { text })
                                    .await.is_err() { return; }
                            }
                        }
                    }
                    audio = audio_in_rx.recv() => {
                        let Some(bytes) = audio else {
                            // Audio side closed — graceful end.
                            let _ = events_tx.send(VoiceEvent::End {
                                reason: ProviderEndReason::Graceful,
                            }).await;
                            return;
                        };
                        if let Some(d) = behaviour.frame_delay {
                            tokio::time::sleep(d).await;
                        }
                        if !first_frame_seen {
                            first_frame_seen = true;
                            if !behaviour.user_transcript.is_empty() {
                                let _ = events_tx.send(VoiceEvent::TranscriptFinal {
                                    role: "user".into(),
                                    text: behaviour.user_transcript.clone(),
                                }).await;
                            }
                            if !behaviour.agent_text.is_empty() {
                                let _ = events_tx.send(VoiceEvent::AgentText {
                                    text: behaviour.agent_text.clone(),
                                }).await;
                            }
                        }
                        // Skip emitting AudioOut while interrupted so
                        // the barge-in test can verify the buffer is
                        // flushed. The latch resets on the next frame.
                        if interrupted {
                            interrupted = false;
                            continue;
                        }
                        let pcm = behaviour
                            .tts_pcm_per_frame
                            .clone()
                            .unwrap_or_else(|| bytes.clone());
                        if events_tx.send(VoiceEvent::AudioOut { pcm_le_bytes: pcm })
                            .await.is_err() { return; }
                    }
                }
            }
        });

        Ok(VoiceProviderSession {
            audio_in_tx,
            control_in_tx,
            events_rx,
            task,
        })
    }
}

/// Drop helper — releases mock state on test teardown so a leaked
/// `Arc<MockEchoProvider>` doesn't keep the tokio runtime alive.
#[cfg(test)]
impl Drop for MockEchoProvider {
    fn drop(&mut self) {
        // Nothing to flush — the per-session tasks own their own
        // resources via the spawn() above. Explicit Drop impl is here
        // as a docstring anchor for iter-5 review: the real adapter's
        // `Drop` should `abort()` any persistent upstream connection.
    }
}

/// Convenience type alias used by the route handler's state.
pub type SharedVoiceProvider = Arc<dyn VoiceProvider>;

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn params(session_id: &str) -> VoiceSessionStartParams {
        VoiceSessionStartParams {
            session_id: session_id.to_string(),
            tenant_id: "default".into(),
            provider_alias: "mock".into(),
            sample_rate_hz_in: 16_000,
            sample_rate_hz_out: 24_000,
            voice_id: None,
        }
    }

    #[tokio::test]
    async fn mock_emits_ready_event_first() {
        let p = MockEchoProvider::new("mock");
        let mut s = p.open(params("voice-1")).await.expect("open");
        let ev = s.events_rx.recv().await.expect("first event");
        match ev {
            VoiceEvent::Ready { provider_session_id } => {
                assert!(provider_session_id.contains("voice-1"));
            }
            other => panic!("expected Ready first; got {other:?}"),
        }
        // Close cleanly so the spawned task doesn't outlive the test.
        let _ = s.control_in_tx.send(ProviderCommand::Close).await;
        let _ = s.task.await;
    }

    #[tokio::test]
    async fn mock_echoes_audio_back_as_audio_out() {
        let p = MockEchoProvider::new("mock");
        let mut s = p.open(params("voice-2")).await.expect("open");
        // Drain Ready.
        let _ = s.events_rx.recv().await;

        s.audio_in_tx
            .send(vec![0xAA, 0xBB, 0xCC, 0xDD])
            .await
            .unwrap();
        let ev = tokio::time::timeout(Duration::from_secs(1), s.events_rx.recv())
            .await
            .expect("got AudioOut within 1s")
            .expect("Some(event)");
        match ev {
            VoiceEvent::AudioOut { pcm_le_bytes } => {
                assert_eq!(pcm_le_bytes, vec![0xAA, 0xBB, 0xCC, 0xDD]);
            }
            other => panic!("expected AudioOut; got {other:?}"),
        }

        let _ = s.control_in_tx.send(ProviderCommand::Close).await;
        let _ = s.task.await;
    }

    #[tokio::test]
    async fn mock_emits_user_transcript_and_agent_text_on_first_frame() {
        let p = MockEchoProvider::with_behaviour(
            "mock",
            MockBehaviour {
                user_transcript: "hello world".into(),
                agent_text: "hi there".into(),
                ..MockBehaviour::default()
            },
        );
        let mut s = p.open(params("voice-3")).await.expect("open");
        let _ = s.events_rx.recv().await; // Ready

        s.audio_in_tx.send(vec![0u8, 0]).await.unwrap();

        // Expect TranscriptFinal then AgentText then AudioOut, in order.
        let mut saw_transcript = false;
        let mut saw_agent_text = false;
        let mut saw_audio = false;
        for _ in 0..3 {
            let ev = tokio::time::timeout(Duration::from_secs(1), s.events_rx.recv())
                .await
                .expect("event arrives")
                .expect("some");
            match ev {
                VoiceEvent::TranscriptFinal { role, text } => {
                    assert_eq!(role, "user");
                    assert_eq!(text, "hello world");
                    saw_transcript = true;
                }
                VoiceEvent::AgentText { text } => {
                    assert_eq!(text, "hi there");
                    saw_agent_text = true;
                }
                VoiceEvent::AudioOut { .. } => {
                    saw_audio = true;
                }
                other => panic!("unexpected: {other:?}"),
            }
        }
        assert!(saw_transcript && saw_agent_text && saw_audio);

        let _ = s.control_in_tx.send(ProviderCommand::Close).await;
        let _ = s.task.await;
    }

    #[tokio::test]
    async fn mock_interrupt_drops_one_audio_out_frame() {
        // After an Interrupt command, the very next inbound audio is
        // not echoed back as AudioOut — the latch resets and the frame
        // after that is echoed as normal. Pins the barge-in contract
        // the iter-6 handler will rely on.
        let p = MockEchoProvider::new("mock");
        let mut s = p.open(params("voice-4")).await.expect("open");
        let _ = s.events_rx.recv().await; // Ready

        // First frame echoes normally.
        s.audio_in_tx.send(vec![1, 2]).await.unwrap();
        let ev = tokio::time::timeout(Duration::from_millis(500), s.events_rx.recv())
            .await
            .unwrap()
            .unwrap();
        assert!(matches!(ev, VoiceEvent::AudioOut { .. }));

        // Interrupt then send another frame — that frame is NOT echoed.
        s.control_in_tx
            .send(ProviderCommand::Interrupt)
            .await
            .unwrap();
        // Give the select! a moment to drain the control channel
        // before the next audio frame races it.
        tokio::time::sleep(Duration::from_millis(20)).await;
        s.audio_in_tx.send(vec![3, 4]).await.unwrap();

        // No AudioOut should arrive within a small window.
        let drained = tokio::time::timeout(Duration::from_millis(150), s.events_rx.recv()).await;
        assert!(
            drained.is_err(),
            "expected no AudioOut after interrupt; got {:?}",
            drained
        );

        // Next frame echoes as normal — interrupt is one-shot.
        s.audio_in_tx.send(vec![5, 6]).await.unwrap();
        let ev = tokio::time::timeout(Duration::from_millis(500), s.events_rx.recv())
            .await
            .unwrap()
            .unwrap();
        assert!(matches!(ev, VoiceEvent::AudioOut { .. }));

        let _ = s.control_in_tx.send(ProviderCommand::Close).await;
        let _ = s.task.await;
    }

    #[tokio::test]
    async fn mock_close_emits_end_event() {
        let p = MockEchoProvider::new("mock");
        let mut s = p.open(params("voice-5")).await.expect("open");
        let _ = s.events_rx.recv().await; // Ready
        s.control_in_tx
            .send(ProviderCommand::Close)
            .await
            .unwrap();
        let ev = tokio::time::timeout(Duration::from_secs(1), s.events_rx.recv())
            .await
            .unwrap()
            .unwrap();
        match ev {
            VoiceEvent::End { reason } => {
                assert_eq!(reason, ProviderEndReason::Graceful);
            }
            other => panic!("expected End; got {other:?}"),
        }
        let _ = s.task.await;
    }

    #[tokio::test]
    async fn mock_approve_tool_emits_agent_text_echo() {
        // The mock translates `ApproveTool` commands into a synthetic
        // AgentText event so the iter-7 approval flow can be exercised
        // end-to-end without a real agent loop. The text encodes the
        // approval id + decision so tests can assert the right pair.
        let p = MockEchoProvider::new("mock");
        let mut s = p.open(params("voice-6")).await.expect("open");
        let _ = s.events_rx.recv().await; // Ready

        s.control_in_tx
            .send(ProviderCommand::ApproveTool {
                approval_id: "ap-1".into(),
                approve: true,
            })
            .await
            .unwrap();
        let ev = tokio::time::timeout(Duration::from_secs(1), s.events_rx.recv())
            .await
            .unwrap()
            .unwrap();
        match ev {
            VoiceEvent::AgentText { text } => assert_eq!(text, "approved:ap-1"),
            other => panic!("expected AgentText; got {other:?}"),
        }

        let _ = s.control_in_tx.send(ProviderCommand::Close).await;
        let _ = s.task.await;
    }

    #[tokio::test]
    async fn mock_open_returns_error_when_configured() {
        let p = MockEchoProvider::with_behaviour(
            "mock",
            MockBehaviour {
                fail_open_with: Some("upstream 401".into()),
                ..MockBehaviour::default()
            },
        );
        let err = p.open(params("voice-7")).await.unwrap_err();
        assert!(matches!(err, ProviderOpenError::Upstream { detail } if detail == "upstream 401"));
    }

    #[test]
    fn voice_event_round_trips_via_serde() {
        // VoiceEvent is `Serialize` so the gateway's debug log
        // sink can dump events. Pin the wire shape so a future
        // refactor doesn't silently rename a discriminant.
        let ev = VoiceEvent::TranscriptPartial {
            role: "user".into(),
            text: "hi".into(),
        };
        let s = serde_json::to_string(&ev).unwrap();
        let v: serde_json::Value = serde_json::from_str(&s).unwrap();
        assert_eq!(v["kind"], "transcript_partial");
        assert_eq!(v["role"], "user");
        assert_eq!(v["text"], "hi");
    }

    #[test]
    fn provider_open_error_unknown_alias_renders() {
        let e = ProviderOpenError::UnknownAlias {
            alias: "ghost".into(),
        };
        assert!(format!("{e}").contains("ghost"));
    }
}
