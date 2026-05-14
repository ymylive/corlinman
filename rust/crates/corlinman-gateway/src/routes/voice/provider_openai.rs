//! OpenAI Realtime API adapter for the voice surface.
//!
//! Iter 5 of D4. Wires the [`provider::VoiceProvider`] trait to
//! `wss://api.openai.com/v1/realtime`. The WebSocket is opened on
//! `open()`; three internal tasks then bridge the gateway's mpsc
//! channels to the upstream socket:
//!
//! 1. **Audio pump**: drains `audio_in_rx` (gateway → provider PCM),
//!    base64-encodes each frame, wraps in
//!    `{"type":"input_audio_buffer.append","audio":"<b64>"}`, and
//!    writes to the upstream WS as a text frame.
//! 2. **Control pump**: translates [`provider::ProviderCommand`] into
//!    the matching upstream control message
//!    (`response.cancel`, `response.create`, `session.update`, …).
//! 3. **Event pump**: reads upstream JSON, classifies the discriminant
//!    (`response.audio.delta`, `conversation.item.input_audio_
//!    transcription.completed`, `response.function_call_arguments.
//!    delta`, `error`, …) and emits semantic [`provider::VoiceEvent`]
//!    values into `events_tx`.
//!
//! ## No real network in default tests
//!
//! Per design: `cargo test --lib voice` MUST NOT attempt a real
//! upstream connection. We achieve that with two seams:
//!
//! - `OpenAIRealtimeAdapter::open` reads `OPENAI_API_KEY` from env and
//!   returns `MissingCredentials` if absent. The default test path
//!   never sets that env, so any test that constructs the adapter
//!   short-circuits to the error branch.
//! - The envelope translation lives in pure functions
//!   ([`encode_input_audio`], [`classify_upstream_event`]) which the
//!   unit tests cover without opening any sockets.
//!
//! A live smoke test against the real OpenAI endpoint lives behind a
//! `#[ignore]` attribute and is executed only when an operator runs
//! `cargo test --lib voice -- --ignored` with `OPENAI_API_KEY` set.

use std::env;

use async_trait::async_trait;
use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use serde_json::{json, Value};

use super::provider::{
    ProviderEndReason, ProviderOpenError, VoiceEvent, VoiceProvider, VoiceProviderSession,
    VoiceSessionStartParams,
};

/// OpenAI Realtime endpoint. The `model` query parameter selects the
/// realtime variant; `gpt-4o-realtime-preview` is the GA-as-of-2025-Q1
/// model id. Operators wanting a different model override via the
/// `OPENAI_REALTIME_MODEL` env var.
pub const OPENAI_REALTIME_URL_BASE: &str = "wss://api.openai.com/v1/realtime";
pub const DEFAULT_OPENAI_REALTIME_MODEL: &str = "gpt-4o-realtime-preview";

/// Adapter alias as registered against `[providers]` config. Operator
/// sets `[voice] provider_alias = "openai-realtime"` and the route
/// resolves to this adapter.
pub const ALIAS: &str = "openai-realtime";

/// The adapter is a thin handle — credentials are read from env per
/// session, not cached, so an operator-level `OPENAI_API_KEY` rotation
/// takes effect on the next session without restarting the gateway.
#[derive(Debug, Default)]
pub struct OpenAIRealtimeAdapter {}

impl OpenAIRealtimeAdapter {
    pub fn new() -> Self {
        Self {}
    }

    /// Build the upstream URL — exposed so the iter-10 e2e harness can
    /// override for a staging endpoint without forking the adapter.
    pub fn upstream_url(model: &str) -> String {
        format!("{OPENAI_REALTIME_URL_BASE}?model={model}")
    }
}

#[async_trait]
impl VoiceProvider for OpenAIRealtimeAdapter {
    fn alias(&self) -> &str {
        ALIAS
    }

    /// Open a session against OpenAI Realtime. Returns:
    ///
    /// - `Err(MissingCredentials)` when `OPENAI_API_KEY` is unset
    ///   (every default test path).
    /// - `Err(Upstream)` when the WebSocket handshake itself fails;
    ///   never reached by tests because of the env gate above.
    /// - `Ok(VoiceProviderSession)` once the upstream socket is
    ///   established. Mid-session failures travel via
    ///   [`VoiceEvent::Error`] / `End`.
    async fn open(
        &self,
        params: VoiceSessionStartParams,
    ) -> Result<VoiceProviderSession, ProviderOpenError> {
        // Env-gate: no key → no session. Keeps tests off the network.
        let api_key =
            env::var("OPENAI_API_KEY").map_err(|_| ProviderOpenError::MissingCredentials {
                detail: "OPENAI_API_KEY env var unset".into(),
            })?;
        let model = env::var("OPENAI_REALTIME_MODEL")
            .unwrap_or_else(|_| DEFAULT_OPENAI_REALTIME_MODEL.into());

        // The actual handshake + bridge lives in [`spawn_session`]
        // (private fn below). Splitting it out keeps the
        // `OPENAI_API_KEY` gate atop this function so iter-5 tests
        // can short-circuit without ever entering the network code.
        spawn_session(api_key, model, params).await
    }
}

/// Live spawn path. **Only reached when `OPENAI_API_KEY` is set**, so
/// the default unit-test runner (which never sets it) never gets here.
///
/// The implementation is intentionally minimal — iter 5's commitment is
/// **the trait wiring + envelope translation**, not a polished prod
/// adapter. iter 10's e2e harness exercises the full handshake against
/// the live endpoint; iter 7 builds tool-call routing on top.
async fn spawn_session(
    api_key: String,
    model: String,
    params: VoiceSessionStartParams,
) -> Result<VoiceProviderSession, ProviderOpenError> {
    use tokio::sync::mpsc;
    use tokio_tungstenite::tungstenite::client::IntoClientRequest;
    use tokio_tungstenite::tungstenite::http::header::{AUTHORIZATION, USER_AGENT};
    use tokio_tungstenite::tungstenite::Message as WsMessage;

    use super::provider::{ProviderCommand, DEFAULT_PROVIDER_CHANNEL_CAPACITY};
    use futures::{SinkExt, StreamExt};

    // Build the upstream request with the OpenAI-required headers.
    // Realtime needs an explicit `OpenAI-Beta: realtime=v1` header in
    // addition to `Authorization: Bearer …`.
    let url = OpenAIRealtimeAdapter::upstream_url(&model);
    let mut request = url
        .into_client_request()
        .map_err(|e| ProviderOpenError::Other {
            detail: format!("build upstream request: {e}"),
        })?;
    let headers = request.headers_mut();
    headers.insert(
        AUTHORIZATION,
        format!("Bearer {api_key}")
            .parse()
            .map_err(|e| ProviderOpenError::Other {
                detail: format!("auth header: {e}"),
            })?,
    );
    headers.insert(
        "openai-beta",
        "realtime=v1".parse().expect("static header parses"),
    );
    headers.insert(
        USER_AGENT,
        "corlinman-gateway/voice"
            .parse()
            .expect("static header parses"),
    );

    let (ws_stream, _resp) = tokio_tungstenite::connect_async(request)
        .await
        .map_err(|e| ProviderOpenError::Upstream {
            detail: format!("openai realtime handshake: {e}"),
        })?;

    let (mut ws_tx, mut ws_rx) = ws_stream.split();

    let (audio_in_tx, mut audio_in_rx) =
        mpsc::channel::<Vec<u8>>(DEFAULT_PROVIDER_CHANNEL_CAPACITY);
    let (control_in_tx, mut control_in_rx) =
        mpsc::channel::<ProviderCommand>(DEFAULT_PROVIDER_CHANNEL_CAPACITY);
    let (events_tx, events_rx) = mpsc::channel::<VoiceEvent>(DEFAULT_PROVIDER_CHANNEL_CAPACITY);

    // Initial session.update: declare the audio formats + voice id so
    // the provider knows what we're sending and what to send back.
    let session_update = build_session_update(&params);
    if let Err(e) = ws_tx.send(WsMessage::Text(session_update)).await {
        return Err(ProviderOpenError::Upstream {
            detail: format!("send session.update: {e}"),
        });
    }

    let session_id = params.session_id.clone();

    // Single tokio task drives both directions because tungstenite's
    // split halves are paired (closing one closes the other). Inside
    // the task we `tokio::select!` between client→provider work and
    // provider→client work.
    let task = tokio::spawn(async move {
        // Emit `Ready` first so the gateway can ack `start` to its
        // client without waiting for the upstream's first event.
        if events_tx
            .send(VoiceEvent::Ready {
                provider_session_id: format!("openai-{session_id}"),
            })
            .await
            .is_err()
        {
            return;
        }

        loop {
            tokio::select! {
                biased;
                cmd = control_in_rx.recv() => {
                    match cmd {
                        Some(ProviderCommand::Close) | None => {
                            let _ = ws_tx.send(WsMessage::Close(None)).await;
                            let _ = events_tx.send(VoiceEvent::End {
                                reason: ProviderEndReason::Graceful,
                            }).await;
                            return;
                        }
                        Some(ProviderCommand::Interrupt) => {
                            let _ = ws_tx.send(WsMessage::Text(
                                json!({"type":"response.cancel"}).to_string()
                            )).await;
                        }
                        Some(ProviderCommand::ApproveTool { .. }) => {
                            // iter-7 wires this through the agent loop
                            // and submits a tool result via
                            // `conversation.item.create`. iter-5 just
                            // logs and forwards nothing.
                            tracing::debug!(
                                target: "voice.openai",
                                "approve_tool received but iter-7 wiring not yet active"
                            );
                        }
                    }
                }
                audio = audio_in_rx.recv() => {
                    let Some(bytes) = audio else {
                        // Audio side closed → commit the buffer and
                        // wait for upstream final events to flush.
                        let _ = ws_tx.send(WsMessage::Text(
                            json!({"type":"input_audio_buffer.commit"}).to_string()
                        )).await;
                        continue;
                    };
                    let envelope = encode_input_audio(&bytes);
                    if let Err(e) = ws_tx.send(WsMessage::Text(envelope)).await {
                        let _ = events_tx.send(VoiceEvent::Error {
                            code: "upstream_write".into(),
                            message: e.to_string(),
                        }).await;
                        let _ = events_tx.send(VoiceEvent::End {
                            reason: ProviderEndReason::ProviderError,
                        }).await;
                        return;
                    }
                }
                up = ws_rx.next() => {
                    match up {
                        None => {
                            let _ = events_tx.send(VoiceEvent::End {
                                reason: ProviderEndReason::ProviderError,
                            }).await;
                            return;
                        }
                        Some(Err(e)) => {
                            let _ = events_tx.send(VoiceEvent::Error {
                                code: "upstream_read".into(),
                                message: e.to_string(),
                            }).await;
                            let _ = events_tx.send(VoiceEvent::End {
                                reason: ProviderEndReason::ProviderError,
                            }).await;
                            return;
                        }
                        Some(Ok(WsMessage::Text(t))) => {
                            for ev in classify_upstream_event(&t) {
                                if events_tx.send(ev).await.is_err() {
                                    return;
                                }
                            }
                        }
                        Some(Ok(WsMessage::Binary(b))) => {
                            // Realtime as of GA sends audio in JSON
                            // base64 envelopes, never raw binary, so
                            // this branch is defensive. Skip silently.
                            tracing::trace!(
                                target: "voice.openai",
                                bytes = b.len(),
                                "unexpected binary frame from upstream; skipping"
                            );
                        }
                        Some(Ok(WsMessage::Close(_))) => {
                            let _ = events_tx.send(VoiceEvent::End {
                                reason: ProviderEndReason::Graceful,
                            }).await;
                            return;
                        }
                        Some(Ok(_)) => { /* Ping/Pong handled by tungstenite */ }
                    }
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

// ---------------------------------------------------------------------------
// Pure envelope translation
// ---------------------------------------------------------------------------

/// Build the initial `session.update` JSON. Pinned audio formats so
/// upstream knows our wire layout matches the spec
/// (`input_audio_format=pcm16`, `output_audio_format=pcm16`).
///
/// `voice_id` is forwarded verbatim if present (design Q3 — corlinman
/// doesn't curate voice catalogues; the operator's `agent_card` value
/// passes through).
pub fn build_session_update(params: &VoiceSessionStartParams) -> String {
    let mut session = json!({
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
        "modalities": ["audio", "text"],
    });
    if let Some(voice) = &params.voice_id {
        session["voice"] = Value::String(voice.clone());
    }
    json!({
        "type": "session.update",
        "session": session,
    })
    .to_string()
}

/// Encode a PCM-16 audio frame into the OpenAI Realtime
/// `input_audio_buffer.append` envelope.
///
/// Pure function so iter-5 tests can pin the wire shape without a
/// network. The envelope is a single JSON text frame; the audio bytes
/// are base64-encoded.
pub fn encode_input_audio(pcm_le_bytes: &[u8]) -> String {
    let b64 = B64.encode(pcm_le_bytes);
    json!({
        "type": "input_audio_buffer.append",
        "audio": b64,
    })
    .to_string()
}

/// Translate one upstream JSON event into zero or more semantic
/// [`VoiceEvent`] values.
///
/// Returns a `Vec` rather than a single event because some upstream
/// events fan out (e.g. a single `response.done` with text + audio
/// would map to `AgentText` + `AudioOut`); iter 5 only handles the
/// common minimum but the shape supports the future cases.
///
/// Unknown / unmapped events return an empty vec — the gateway logs
/// at trace level upstream.
pub fn classify_upstream_event(json_text: &str) -> Vec<VoiceEvent> {
    let v: Value = match serde_json::from_str(json_text) {
        Ok(v) => v,
        Err(_) => return vec![],
    };
    let kind = match v.get("type").and_then(|t| t.as_str()) {
        Some(s) => s,
        None => return vec![],
    };
    match kind {
        // Base-64 audio chunk in `delta`.
        "response.audio.delta" => {
            let Some(delta) = v.get("delta").and_then(|d| d.as_str()) else {
                return vec![];
            };
            match B64.decode(delta) {
                Ok(bytes) => vec![VoiceEvent::AudioOut {
                    pcm_le_bytes: bytes,
                }],
                Err(_) => vec![],
            }
        }
        // Assistant text — fires alongside or before audio depending
        // on modality. We forward immediately so clients can start
        // rendering captions before the TTS finishes.
        "response.text.delta" | "response.audio_transcript.delta" => {
            let Some(t) = v.get("delta").and_then(|d| d.as_str()) else {
                return vec![];
            };
            vec![VoiceEvent::AgentText {
                text: t.to_string(),
            }]
        }
        // User ASR — provider's transcription of inbound audio.
        "conversation.item.input_audio_transcription.delta" => {
            let Some(t) = v.get("delta").and_then(|d| d.as_str()) else {
                return vec![];
            };
            vec![VoiceEvent::TranscriptPartial {
                role: "user".into(),
                text: t.to_string(),
            }]
        }
        "conversation.item.input_audio_transcription.completed" => {
            let Some(t) = v.get("transcript").and_then(|d| d.as_str()) else {
                return vec![];
            };
            vec![VoiceEvent::TranscriptFinal {
                role: "user".into(),
                text: t.to_string(),
            }]
        }
        // Tool call. iter 7 will plug into the approval gate; iter 5
        // just maps the envelope so the shape is testable.
        "response.function_call_arguments.done" => {
            let call_id = v
                .get("call_id")
                .and_then(|c| c.as_str())
                .unwrap_or_default()
                .to_string();
            let tool = v
                .get("name")
                .and_then(|n| n.as_str())
                .unwrap_or_default()
                .to_string();
            // Args arrive as a JSON string; round-trip into a Value so
            // downstream consumers don't have to re-parse.
            let args = v
                .get("arguments")
                .and_then(|a| a.as_str())
                .and_then(|s| serde_json::from_str::<Value>(s).ok())
                .unwrap_or(Value::Null);
            vec![VoiceEvent::ToolCall {
                call_id,
                tool,
                args,
            }]
        }
        // Provider error. The gateway closes the session afterwards.
        "error" => {
            let code = v
                .get("error")
                .and_then(|e| e.get("code"))
                .and_then(|c| c.as_str())
                .unwrap_or("unknown")
                .to_string();
            let message = v
                .get("error")
                .and_then(|e| e.get("message"))
                .and_then(|m| m.as_str())
                .unwrap_or("upstream error")
                .to_string();
            vec![VoiceEvent::Error { code, message }]
        }
        // Done events that don't carry a payload we forward.
        "session.created"
        | "session.updated"
        | "input_audio_buffer.committed"
        | "response.created"
        | "response.done"
        | "response.audio.done"
        | "response.audio_transcript.done"
        | "response.text.done"
        | "rate_limits.updated" => vec![],
        // Any other type: not classified yet. Vec empty so the bridge
        // doesn't forward unknown shapes; iter 7+ will add as needed.
        _ => vec![],
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn params() -> VoiceSessionStartParams {
        VoiceSessionStartParams {
            session_id: "voice-1".into(),
            tenant_id: "default".into(),
            provider_alias: ALIAS.into(),
            sample_rate_hz_in: 16_000,
            sample_rate_hz_out: 24_000,
            voice_id: Some("alloy".into()),
        }
    }

    #[test]
    fn alias_is_canonical() {
        assert_eq!(ALIAS, "openai-realtime");
        let a = OpenAIRealtimeAdapter::new();
        assert_eq!(a.alias(), ALIAS);
    }

    #[test]
    fn upstream_url_carries_model() {
        let url = OpenAIRealtimeAdapter::upstream_url(DEFAULT_OPENAI_REALTIME_MODEL);
        assert!(url.starts_with("wss://api.openai.com/v1/realtime?model="));
        assert!(url.contains("gpt-4o-realtime-preview"));
    }

    #[test]
    fn session_update_pins_audio_formats_and_voice() {
        let s = build_session_update(&params());
        let v: Value = serde_json::from_str(&s).unwrap();
        assert_eq!(v["type"], "session.update");
        assert_eq!(v["session"]["input_audio_format"], "pcm16");
        assert_eq!(v["session"]["output_audio_format"], "pcm16");
        assert_eq!(v["session"]["voice"], "alloy");
    }

    #[test]
    fn session_update_omits_voice_when_unset() {
        let mut p = params();
        p.voice_id = None;
        let s = build_session_update(&p);
        let v: Value = serde_json::from_str(&s).unwrap();
        assert!(v["session"].get("voice").is_none());
    }

    #[test]
    fn encode_input_audio_wraps_pcm_in_b64_envelope() {
        let env_text = encode_input_audio(&[0xDE, 0xAD, 0xBE, 0xEF]);
        let v: Value = serde_json::from_str(&env_text).unwrap();
        assert_eq!(v["type"], "input_audio_buffer.append");
        // base64("DEADBEEF") = "3q2+7w=="
        assert_eq!(v["audio"], "3q2+7w==");
    }

    #[test]
    fn classify_audio_delta_yields_audio_out() {
        // base64("hello") = "aGVsbG8="
        let upstream = json!({
            "type": "response.audio.delta",
            "delta": "aGVsbG8=",
        })
        .to_string();
        let evs = classify_upstream_event(&upstream);
        assert_eq!(evs.len(), 1);
        match &evs[0] {
            VoiceEvent::AudioOut { pcm_le_bytes } => {
                assert_eq!(pcm_le_bytes, &b"hello".to_vec());
            }
            other => panic!("expected AudioOut; got {other:?}"),
        }
    }

    #[test]
    fn classify_audio_delta_invalid_b64_drops() {
        let upstream = json!({
            "type": "response.audio.delta",
            "delta": "not-valid-base64!!!",
        })
        .to_string();
        let evs = classify_upstream_event(&upstream);
        assert!(evs.is_empty(), "invalid base64 must not surface as event");
    }

    #[test]
    fn classify_text_delta_yields_agent_text() {
        let upstream = json!({
            "type": "response.text.delta",
            "delta": "hello there",
        })
        .to_string();
        let evs = classify_upstream_event(&upstream);
        match &evs[0] {
            VoiceEvent::AgentText { text } => assert_eq!(text, "hello there"),
            other => panic!("expected AgentText; got {other:?}"),
        }
    }

    #[test]
    fn classify_input_transcription_delta_yields_partial() {
        let upstream = json!({
            "type": "conversation.item.input_audio_transcription.delta",
            "delta": "hel",
        })
        .to_string();
        let evs = classify_upstream_event(&upstream);
        match &evs[0] {
            VoiceEvent::TranscriptPartial { role, text } => {
                assert_eq!(role, "user");
                assert_eq!(text, "hel");
            }
            other => panic!("expected TranscriptPartial; got {other:?}"),
        }
    }

    #[test]
    fn classify_input_transcription_completed_yields_final() {
        let upstream = json!({
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "hello world",
        })
        .to_string();
        let evs = classify_upstream_event(&upstream);
        match &evs[0] {
            VoiceEvent::TranscriptFinal { role, text } => {
                assert_eq!(role, "user");
                assert_eq!(text, "hello world");
            }
            other => panic!("expected TranscriptFinal; got {other:?}"),
        }
    }

    #[test]
    fn classify_function_call_arguments_done_yields_tool_call() {
        let upstream = json!({
            "type": "response.function_call_arguments.done",
            "call_id": "call_abc",
            "name": "web_search",
            "arguments": "{\"query\":\"corlinman\"}",
        })
        .to_string();
        let evs = classify_upstream_event(&upstream);
        match &evs[0] {
            VoiceEvent::ToolCall {
                call_id,
                tool,
                args,
            } => {
                assert_eq!(call_id, "call_abc");
                assert_eq!(tool, "web_search");
                assert_eq!(args["query"], "corlinman");
            }
            other => panic!("expected ToolCall; got {other:?}"),
        }
    }

    #[test]
    fn classify_function_call_args_unparseable_falls_back_to_null() {
        let upstream = json!({
            "type": "response.function_call_arguments.done",
            "call_id": "x",
            "name": "tool",
            "arguments": "not-json",
        })
        .to_string();
        let evs = classify_upstream_event(&upstream);
        match &evs[0] {
            VoiceEvent::ToolCall { args, .. } => {
                assert_eq!(args, &Value::Null);
            }
            other => panic!("expected ToolCall; got {other:?}"),
        }
    }

    #[test]
    fn classify_error_event_yields_error() {
        let upstream = json!({
            "type": "error",
            "error": {
                "code": "rate_limit_exceeded",
                "message": "too many requests",
            }
        })
        .to_string();
        let evs = classify_upstream_event(&upstream);
        match &evs[0] {
            VoiceEvent::Error { code, message } => {
                assert_eq!(code, "rate_limit_exceeded");
                assert_eq!(message, "too many requests");
            }
            other => panic!("expected Error; got {other:?}"),
        }
    }

    #[test]
    fn classify_lifecycle_events_drop_silently() {
        // session.created etc. are bookkeeping; not forwarded to client.
        for kind in [
            "session.created",
            "session.updated",
            "response.created",
            "response.done",
            "rate_limits.updated",
        ] {
            let upstream = json!({"type": kind}).to_string();
            assert!(
                classify_upstream_event(&upstream).is_empty(),
                "lifecycle event {kind} must not produce a VoiceEvent"
            );
        }
    }

    #[test]
    fn classify_unknown_event_drops() {
        let upstream = json!({"type": "future.event.we.havent.mapped"}).to_string();
        assert!(classify_upstream_event(&upstream).is_empty());
    }

    #[test]
    fn classify_malformed_json_drops() {
        // Non-JSON payload from upstream must not panic the bridge.
        assert!(classify_upstream_event("not-json").is_empty());
        assert!(classify_upstream_event("[]").is_empty()); // missing type
        assert!(classify_upstream_event("{}").is_empty());
    }

    #[tokio::test]
    async fn open_without_api_key_returns_missing_credentials() {
        // Default unit-test path. Explicitly clear the env var in case
        // the developer's shell has it set; restore-on-drop pattern.
        struct EnvGuard {
            previous: Option<String>,
        }
        impl Drop for EnvGuard {
            fn drop(&mut self) {
                match &self.previous {
                    Some(v) => unsafe { std::env::set_var("OPENAI_API_KEY", v) },
                    None => unsafe { std::env::remove_var("OPENAI_API_KEY") },
                }
            }
        }
        let _guard = EnvGuard {
            previous: std::env::var("OPENAI_API_KEY").ok(),
        };
        unsafe {
            std::env::remove_var("OPENAI_API_KEY");
        }

        let a = OpenAIRealtimeAdapter::new();
        let err = a.open(params()).await.unwrap_err();
        assert!(
            matches!(err, ProviderOpenError::MissingCredentials { .. }),
            "got {err:?}"
        );
    }

    /// Live-network smoke test against api.openai.com. **#[ignore] by
    /// default**; operators run via `cargo test -p corlinman-gateway
    /// --lib voice -- --ignored` with `OPENAI_API_KEY` set. CI's
    /// nightly job is the intended trigger; the default `cargo test`
    /// run never touches the network.
    #[tokio::test]
    #[ignore = "live OpenAI Realtime API; requires OPENAI_API_KEY and outbound network"]
    async fn live_smoke_handshake_succeeds() {
        if std::env::var("OPENAI_API_KEY").is_err() {
            // Operator forgot the env even though they passed --ignored.
            // Skip rather than fail noisily — a missing key is "not
            // configured", not "broken".
            eprintln!("OPENAI_API_KEY unset; skipping live smoke");
            return;
        }
        let a = OpenAIRealtimeAdapter::new();
        let session = a.open(params()).await.expect("open succeeds");
        // Don't push real audio — just ack Ready and close, keeping
        // the smoke test bounded in cost (single connection, no audio
        // frames billed).
        drop(session.audio_in_tx);
        let _ = session
            .control_in_tx
            .send(super::super::provider::ProviderCommand::Close)
            .await;
        let _ = session.task.await;
    }
}
