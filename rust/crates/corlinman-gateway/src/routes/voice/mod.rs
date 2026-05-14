//! `GET /voice` — realtime audio WebSocket (Phase 4 W4 D4 alpha).
//!
//! D4 lands the first full-duplex chat surface. The route is **gated** —
//! every minute on the wire bills cents not micro-cents, so:
//!
//!   1. `[voice] enabled = false` (default) → `503 voice_disabled` with
//!      `Retry-After: 86400` so monitors don't hammer.
//!   2. WebSocket upgrade + `corlinman.voice.v1` subprotocol
//!      negotiation. Client sends a non-matching subprotocol → 400.
//!   3. iter 3 wires the per-tenant cost gate (budget check at
//!      session start, hard-kill at `max_session_seconds`).
//!   4. iter 4+ wires the actual provider WebSocket bridging.
//!
//! Iter 2 ships the upgrade handler that closes the socket immediately
//! after sending a `started` JSON event. Iter 4+ extends the closure
//! to the full bridge loop.
//!
//! See `docs/design/phase4-w4-d4-design.md`.

pub mod approval;
pub mod bridge;
pub mod budget;
pub mod cost;
pub mod framing;
pub mod persistence;
pub mod provider;
pub mod provider_openai;

use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};

use arc_swap::ArcSwap;
use async_trait::async_trait;
use axum::{
    extract::{
        ws::{CloseFrame, Message, WebSocket, WebSocketUpgrade},
        State,
    },
    http::{header, HeaderMap, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    routing::get,
    Json, Router,
};
use corlinman_core::config::Config;
use serde_json::json;
use tokio_util::sync::CancellationToken;
use tracing::{debug, warn};

use crate::middleware::approval::ApprovalGate;
use approval::VoiceApprovalBridge;
use bridge::{run_bridge, BridgeContext, BridgeInFrame, BridgeIo, BridgeIoError, BridgeOutFrame};
use budget::BudgetEnforcer;
use cost::{
    evaluate_budget, next_utc_midnight, utc_day_epoch, BudgetDecision, BudgetDenyReason,
    InMemoryVoiceSpend, VoiceSpend,
};
use framing::{
    accept_subprotocol, encode_server_control, parse_client_control, ClientControl, ServerControl,
    SubprotocolDecision,
};
use persistence::{audio_path_for, SharedTranscriptSink, SharedVoiceSessionStore};
use provider::SharedVoiceProvider;

/// State carried by the `/voice` route.
///
/// - `config` — live `ArcSwap` so flipping `[voice] enabled` /
///   budget knobs at runtime takes effect without restart.
/// - `spend` — process-local minute counter (iter 3); iter 8 swaps
///   to a SQLite-backed `voice_spend` table without touching this
///   handler.
/// - `tenant_id_for_request` — resolver hook so multi-tenant
///   deployments can scope the budget per tenant. The default
///   resolver returns `"default"` matching the schema-level fallback
///   from Phase 4 W1 4-1A; iter 4+ wires the real header / session
///   token plumbing.
#[derive(Clone)]
pub struct VoiceState {
    pub config: Arc<ArcSwap<Config>>,
    pub spend: Arc<dyn VoiceSpend>,
    /// Iter 4+ pluggable provider adapter. `None` keeps the iter-2
    /// stub close path intact for tests that only exercise the gate
    /// layers (flag, subprotocol, budget) without a provider; iter 5
    /// flips to `Some(OpenAIRealtimeProvider)` when `OPENAI_API_KEY`
    /// is set.
    pub provider: Option<SharedVoiceProvider>,
    /// Iter 6+ voice session row store. `None` skips persistence —
    /// useful for tests that only assert the wire shape. Production
    /// constructs an `Arc<SqliteVoiceSessionStore>` against the same
    /// `sessions.sqlite` file path the chat session store uses.
    pub session_store: Option<SharedVoiceSessionStore>,
    /// Iter 6+ transcript bridge. `None` falls back to a no-op
    /// (transcript text is still written to `voice_sessions
    /// .transcript_text` on close). When `Some`, each
    /// `transcript_final` event also appends a `user` / `assistant`
    /// row to the chat session table so the agent loop sees voice
    /// turns indistinguishably from typed turns.
    pub transcript_sink: Option<SharedTranscriptSink>,
    /// Iter 7+ tool-approval gate. `None` keeps the chat-side gate
    /// untouched and makes voice tool-calls auto-approve (matches the
    /// chat path's `NoMatch → Approved` default). When `Some`, every
    /// `VoiceEvent::ToolCall` from the provider is filtered through
    /// the same `pending_approvals` queue that text chat uses, so an
    /// operator decides voice + chat tool calls from one admin UI.
    pub approval_gate: Option<Arc<ApprovalGate>>,
}

impl VoiceState {
    pub fn new(config: Arc<ArcSwap<Config>>) -> Self {
        Self {
            config,
            spend: Arc::new(InMemoryVoiceSpend::new()),
            provider: None,
            session_store: None,
            transcript_sink: None,
            approval_gate: None,
        }
    }

    /// Test seam for plumbing a custom spend store (e.g. one
    /// pre-populated with day usage to exercise the over-budget
    /// branch).
    pub fn with_spend(config: Arc<ArcSwap<Config>>, spend: Arc<dyn VoiceSpend>) -> Self {
        Self {
            config,
            spend,
            provider: None,
            session_store: None,
            transcript_sink: None,
            approval_gate: None,
        }
    }

    /// Wire a provider adapter in. Iter 4 ships the trait + mock; iter
    /// 5 wires the real OpenAI Realtime adapter; iter 6+ uses this
    /// seam to drive transcript persistence + audio retention.
    pub fn with_provider(mut self, provider: SharedVoiceProvider) -> Self {
        self.provider = Some(provider);
        self
    }

    /// Iter 6: wire the voice-session row store. Production passes a
    /// `SqliteVoiceSessionStore` opened against the same per-tenant
    /// `sessions.sqlite` file the chat session store uses; tests pass
    /// the in-memory variant.
    pub fn with_session_store(mut self, store: SharedVoiceSessionStore) -> Self {
        self.session_store = Some(store);
        self
    }

    /// Iter 6: wire the transcript bridge so `transcript_final` events
    /// also flush into the chat sessions table. iter 7+ provides the
    /// concrete adapter that wraps `corlinman-core`'s `SessionStore`
    /// without the route handler ever taking a direct dependency.
    pub fn with_transcript_sink(mut self, sink: SharedTranscriptSink) -> Self {
        self.transcript_sink = Some(sink);
        self
    }

    /// Iter 7: wire the shared `ApprovalGate` so voice tool-calls go
    /// through the same `pending_approvals` queue as the chat surface.
    /// Construction site is `gateway/src/routes/mod.rs` where the
    /// gate is built once per process and shared with `AdminState`.
    pub fn with_approval_gate(mut self, gate: Arc<ApprovalGate>) -> Self {
        self.approval_gate = Some(gate);
        self
    }
}

/// Best-effort tenant resolution from request headers. Iter 3 ships a
/// minimal version: an explicit `X-Tenant-Id` header wins, otherwise
/// fall back to the configured default tenant slug. Iter 4+ replaces
/// this with the real session-token / multi-tenant middleware lookup.
fn resolve_tenant(headers: &HeaderMap, cfg: &Config) -> String {
    if let Some(v) = headers.get("x-tenant-id").and_then(|v| v.to_str().ok()) {
        if !v.trim().is_empty() {
            return v.trim().to_string();
        }
    }
    cfg.tenants.default.clone()
}

fn now_unix_secs() -> u64 {
    SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_secs())
        // The platform clock can't be before the epoch in practice; if
        // it is, the budget gate degrades to a max-session-only check
        // (returning 0 makes today the unix epoch which is harmless
        // for budget arithmetic).
        .unwrap_or(0)
}

/// Stub router used by the legacy [`super::router`] composition that
/// has no live config wired. Always returns 503 — there's no way to
/// enable voice without a config snapshot.
pub fn router() -> Router {
    Router::new().route("/voice", get(voice_disabled_stub))
}

/// Production router. The handler reads the live `[voice]` config
/// snapshot on every request so a hot-reload toggling
/// `enabled = true/false` takes effect on the next connect.
pub fn router_with_state(state: VoiceState) -> Router {
    Router::new()
        .route("/voice", get(voice_handler))
        .with_state(state)
}

async fn voice_disabled_stub() -> Response {
    voice_disabled_response()
}

/// Live `/voice` handler. Responsibilities (iter 2):
///
/// 1. Refuse if `[voice] enabled = false` → 503 + Retry-After.
/// 2. Negotiate the `corlinman.voice.v1` subprotocol from the
///    client-supplied `Sec-WebSocket-Protocol` header.
///    Mismatch → 400 (the design says the upgrade itself MUST NOT
///    succeed against a non-matching subprotocol — close code 1002 is
///    only used when negotiation fails *after* upgrade, which can't
///    happen here since we reject pre-upgrade).
/// 3. On accept, perform the upgrade and run [`run_voice_session`].
///    Iter 2's session is intentionally minimal: send a `started`
///    event then close with code 1000. Iter 4+ wires the real bridge.
///
/// Extractor order matters: `Option<WebSocketUpgrade>` is fallible at
/// the protocol level (returns `None` for non-WS requests) but never
/// rejects the request body. We need our config / subprotocol checks
/// to run **before** the upgrade is attempted so a disabled voice
/// route still 503s for plain GETs and a missing subprotocol still
/// 400s rather than the extractor's default 426 / `connection-not-
/// upgradable`. Wrapping in `Option<…>` and matching `None` in the
/// final branch gives us that ordering for free.
async fn voice_handler(
    State(state): State<VoiceState>,
    headers: HeaderMap,
    ws: Option<WebSocketUpgrade>,
) -> Response {
    let snap = state.config.load();
    let enabled = snap.voice.as_ref().map(|v| v.enabled).unwrap_or(false);

    if !enabled {
        return voice_disabled_response();
    }

    let offered = headers
        .get(http::header::SEC_WEBSOCKET_PROTOCOL)
        .and_then(|v| v.to_str().ok());
    let decision = accept_subprotocol(offered);
    let accepted = match decision {
        SubprotocolDecision::Accept(p) => p,
        SubprotocolDecision::Reject(reason) => {
            warn!(target: "voice", offered = ?offered, reason = %reason, "voice subprotocol negotiation rejected");
            return subprotocol_rejected_response(&reason);
        }
    };

    // ---- iter 3: per-tenant minutes-per-day budget gate ---------------
    //
    // Runs **before** the upgrade attempt so an over-budget tenant
    // gets a clean HTTP 429 instead of a half-opened WebSocket. The
    // unwrap is safe: the early `enabled` check above guarantees
    // `snap.voice` is `Some`. Defensive `clone()` so we drop the
    // ArcSwap guard before doing any blocking spend-store work.
    let voice_cfg = snap
        .voice
        .as_ref()
        .expect("voice config must be present once enabled was true")
        .clone();
    let tenant = resolve_tenant(&headers, &snap);
    let now = now_unix_secs();
    let day_epoch = utc_day_epoch(now);
    let reset_at = next_utc_midnight(now);
    let today = state.spend.snapshot(&tenant, day_epoch);
    match evaluate_budget(&voice_cfg, today, reset_at) {
        BudgetDecision::Allow { .. } => {}
        BudgetDecision::Deny { reason, reset_at } => {
            warn!(
                target: "voice",
                tenant = %tenant,
                day_epoch,
                ?reason,
                "voice session refused: budget gate"
            );
            return budget_exhausted_response(&reason, reset_at);
        }
    }

    // At this point the flag is on AND the subprotocol matches AND the
    // tenant has budget. The request still has to be a real WebSocket
    // upgrade — a plain GET with the right `Sec-WebSocket-Protocol`
    // header but no `Upgrade: websocket` is a malformed client, not a
    // security event.
    let ws = match ws {
        Some(ws) => ws,
        None => return upgrade_required_response(),
    };

    // Record the attempt in the spend store so iter 8's audit table
    // sees one row per session-start regardless of subsequent failure
    // (provider unavailable, immediate disconnect, etc.).
    let _post = state.spend.record_session_start(&tenant, day_epoch);

    let provider_alias = voice_cfg.provider_alias.clone();
    let session_state = state.clone();
    let voice_cfg_for_session = voice_cfg.clone();
    // Snapshot data_dir up here so the session driver can compute the
    // retention path without holding the ArcSwap guard across the
    // upgrade. A hot-flip of `data_dir` mid-session would only affect
    // the next session.
    let data_dir = snap.server.data_dir.clone();
    ws.protocols([accepted]).on_upgrade(move |socket| {
        run_voice_session(
            socket,
            provider_alias,
            session_state,
            voice_cfg_for_session,
            tenant,
            day_epoch,
            data_dir,
        )
    })
}

/// Voice session driver. Two modes:
///
/// 1. **Stub** (iter 2 fallback): when `state.provider` is `None`,
///    sends a single `started` event then closes with code 1000.
///    Tests that exercise gate / framing without a provider use
///    this path; production must wire a provider via
///    [`VoiceState::with_provider`].
/// 2. **Live bridge** (iter 9): when a provider is wired, hands off
///    to [`bridge::run_bridge`] with a [`WebSocketBridgeIo`] adapter
///    and a fully-built [`BridgeContext`]. The bridge owns the pump
///    loop, budget ticker, transcript sink writes, and approval
///    pause logic; this fn is just the construction site.
///
/// **Iter 10 close-outs**:
///
/// 1. The handler **pre-reads the inbound `start` control frame**
///    before constructing the [`BridgeContext`]. The frame's
///    `session_key` and `agent_id` flow into the context so the
///    transcript sink writes turns under the chat-session row the
///    client supplied — fixing the iter-9 fallback that always wrote
///    under the synthetic `session_id`. A first frame that is **not**
///    a `start` is tolerated (the design says `start` is mandatory but
///    a misbehaving client shouldn't crash the route): we fall back to
///    `session_id` and forward the offending frame into the bridge so
///    the bridge's existing protocol-error path can surface it.
/// 2. When `[voice] retain_audio = true`, the handler resolves the
///    on-disk PCM path via [`audio_path_for`] and threads it into
///    [`BridgeContext::audio_path`]. The bridge writes that string
///    into `voice_sessions.audio_path` on session close, fixing the
///    iter-9 hardcoded `None`.
async fn run_voice_session(
    mut socket: WebSocket,
    provider_alias: String,
    state: VoiceState,
    voice_cfg: corlinman_core::config::VoiceConfig,
    tenant: String,
    day_epoch: u64,
    data_dir: std::path::PathBuf,
) {
    let session_id = format!("voice-{}", uuid::Uuid::new_v4());

    let Some(provider) = state.provider.clone() else {
        // Iter-2 stub fallback. The handler still sends `started`
        // so a client probing without a configured provider knows
        // the gateway is alive and the negotiation succeeded.
        let started = encode_server_control(&ServerControl::Started {
            session_id: session_id.clone(),
            provider: provider_alias,
        });
        if let Err(e) = socket.send(Message::Text(started)).await {
            debug!(target: "voice", session_id, err = %e, "failed to send started event; closing");
            return;
        }
        let close = CloseFrame {
            code: 1000,
            reason: "voice provider not configured".into(),
        };
        let _ = socket.send(Message::Close(Some(close))).await;
        return;
    };

    // ---- iter 10 fix #1: pre-read the `start` control frame -------
    //
    // The bridge's pump loop tolerates a redundant `start` mid-session
    // as a no-op, so consuming the first frame here is safe. We give
    // the client up to 5s to send `start`; clients that take longer
    // are doing something pathological and the session times out.
    //
    // If the first frame is anything other than `start`, the design
    // says `start` is required first — but rather than add a new
    // close-code-vs-tolerate decision here we keep the iter-9 fallback
    // (session_key = session_id) and stash the offending frame so the
    // bridge can react to it. Either way, downstream construction is
    // unblocked.
    let (start_session_key, start_agent_id, start_sample_rate, replay_first) =
        match tokio::time::timeout(Duration::from_secs(5), socket.recv()).await {
            Ok(Some(Ok(Message::Text(t)))) => match parse_client_control(&t) {
                Ok(ClientControl::Start {
                    session_key,
                    agent_id,
                    sample_rate_hz,
                    ..
                }) => (Some(session_key), agent_id, Some(sample_rate_hz), None),
                Ok(_other) => {
                    // Non-start text frame: keep the iter-9 fallback.
                    // Replay the original text so the bridge sees it.
                    (None, None, None, Some(BridgeInFrame::Text(t)))
                }
                Err(err) => {
                    // Parse failure: surface to the client and keep going
                    // with the fallback so the bridge's tolerant
                    // protocol-error path can decide whether to terminate.
                    debug!(
                        target: "voice", session_id, err = %err,
                        "first text frame failed to parse; falling back to session_id"
                    );
                    (None, None, None, Some(BridgeInFrame::Text(t)))
                }
            },
            Ok(Some(Ok(Message::Binary(b)))) => {
                // Audio before start. Same fallback — bridge's binary path
                // will rate-limit / validate as normal.
                (None, None, None, Some(BridgeInFrame::Binary(b)))
            }
            Ok(Some(Ok(Message::Close(_))))
            | Ok(Some(Ok(Message::Ping(_))))
            | Ok(Some(Ok(Message::Pong(_))))
            | Ok(Some(Err(_)))
            | Ok(None)
            | Err(_) => {
                // Client hung up / errored / didn't send anything within
                // 5s. We still need to drive the bridge so the session row
                // gets a `start_failed`-style end reason — but with no
                // start frame the safest action is to close immediately
                // before opening a provider session.
                let close = CloseFrame {
                    code: 1002,
                    reason: "missing start frame".into(),
                };
                let _ = socket.send(Message::Close(Some(close))).await;
                return;
            }
        };

    // Per-session cancel token; signals graceful shutdown to the
    // bridge if the gateway-level lifecycle cancels (e.g. process
    // shutdown). The route handler doesn't currently propagate a
    // shutdown token down — when it does, this is the join point.
    let cancel = CancellationToken::new();

    // session_key: from the start frame when present, otherwise the
    // synthetic id. Used for both the chat-session FK and the approval
    // bridge's session-scoped allowlist match.
    let session_key = start_session_key.unwrap_or_else(|| session_id.clone());

    // Approval bridge: if the gateway has a real ApprovalGate,
    // every voice tool-call goes through it; otherwise the bridge
    // auto-approves (matches the chat-side `NoMatch → Approved`
    // default). Use the resolved session_key so an operator's
    // `allow_session_keys = ["my-trusted-session"]` rule fires.
    let approval_bridge = match state.approval_gate.clone() {
        Some(gate) => VoiceApprovalBridge::with_gate(gate, session_key.clone()),
        None => VoiceApprovalBridge::no_gate(session_key.clone()),
    };

    let started_at_instant = Instant::now();
    let started_at_unix = now_unix_secs() as i64;
    let budget = BudgetEnforcer::start(
        &voice_cfg,
        state.spend.clone(),
        tenant.clone(),
        day_epoch,
        started_at_instant,
    );

    // ---- iter 10 fix #2: opt-in audio retention path ---------------
    //
    // `[voice] retain_audio = true` resolves the per-tenant tree;
    // `false` (default) leaves audio_path = None so the row column
    // stays NULL. Path resolution is pure (no mkdir / no file open),
    // so the row is always consistent with operator intent even if a
    // future iter wires the actual byte-stream writer.
    let audio_path = if voice_cfg.retain_audio {
        Some(
            audio_path_for(&data_dir, &tenant, &session_id)
                .to_string_lossy()
                .into_owned(),
        )
    } else {
        None
    };

    // Sample-rate negotiation: client's `start.sample_rate_hz` wins
    // over the config default when present. The provider may still
    // resample upstream if its model expects 24 kHz only.
    let sample_rate_hz_in = start_sample_rate.unwrap_or(voice_cfg.sample_rate_hz_in);

    let ctx = BridgeContext {
        session_id: session_id.clone(),
        provider_alias: provider_alias.clone(),
        tenant_id: tenant,
        day_epoch,
        started_at_unix,
        started_at_instant,
        // iter 10: session_key now flows from the inbound `start`
        // frame when present, falling back to session_id only when the
        // client didn't send one (or sent something else first).
        session_key,
        agent_id: start_agent_id,
        sample_rate_hz_in,
        sample_rate_hz_out: voice_cfg.sample_rate_hz_out,
        voice_id: None,
        provider,
        session_store: state.session_store.clone(),
        transcript_sink: state.transcript_sink.clone(),
        approval_bridge,
        budget,
        audio_path,
        // Production tick = 1 Hz per design.
        tick_interval: Duration::from_secs(1),
        cancel,
    };

    // The bridge expects to drive the inbound stream itself; if the
    // first frame was non-`start` we feed it back through a wrapper
    // I/O that yields the buffered frame once before falling through
    // to the live socket.
    let outcome = match replay_first {
        Some(first) => run_bridge(ReplayWebSocketBridgeIo::new(socket, first), ctx).await,
        None => run_bridge(WebSocketBridgeIo::new(socket), ctx).await,
    };
    debug!(
        target: "voice",
        session_id, end_reason = ?outcome.end_reason,
        duration_secs = outcome.duration_secs, "voice session closed"
    );
}

// ---------------------------------------------------------------------------
// Bridge I/O adapter for axum's WebSocket
// ---------------------------------------------------------------------------

/// Wraps an [`axum::extract::ws::WebSocket`] to fit the bridge's
/// generic [`BridgeIo`] surface. Axum's WebSocket is split into
/// inbound `next()` and outbound `send()` halves; we keep both on the
/// same struct because the bridge always serialises the two anyway.
struct WebSocketBridgeIo {
    socket: WebSocket,
}

impl WebSocketBridgeIo {
    fn new(socket: WebSocket) -> Self {
        Self { socket }
    }
}

#[async_trait]
impl BridgeIo for WebSocketBridgeIo {
    async fn recv(&mut self) -> Option<BridgeInFrame> {
        loop {
            match self.socket.recv().await {
                Some(Ok(Message::Text(t))) => return Some(BridgeInFrame::Text(t)),
                Some(Ok(Message::Binary(b))) => return Some(BridgeInFrame::Binary(b)),
                Some(Ok(Message::Close(_))) => return Some(BridgeInFrame::ClosedByClient),
                Some(Ok(Message::Ping(_))) | Some(Ok(Message::Pong(_))) => {
                    // Keep-alives — let axum auto-respond and keep
                    // looping for the next real frame.
                    continue;
                }
                Some(Err(e)) => {
                    debug!(target: "voice", err = %e, "websocket recv errored; treating as close");
                    return None;
                }
                None => return None,
            }
        }
    }

    async fn send(&mut self, frame: BridgeOutFrame) -> Result<(), BridgeIoError> {
        let msg = match frame {
            BridgeOutFrame::Text(t) => Message::Text(t),
            BridgeOutFrame::Binary(b) => Message::Binary(b),
            BridgeOutFrame::Close { code, reason } => Message::Close(Some(CloseFrame {
                code,
                reason: reason.into(),
            })),
        };
        self.socket.send(msg).await.map_err(|_| BridgeIoError)
    }
}

/// Same as [`WebSocketBridgeIo`] but yields one buffered inbound frame
/// before falling through to the live socket. Used when the route
/// handler pre-consumed the first frame for `start` extraction but the
/// frame turned out to be non-`start` (so the bridge still needs to
/// see it for protocol-error handling).
struct ReplayWebSocketBridgeIo {
    socket: WebSocket,
    pending: Option<BridgeInFrame>,
}

impl ReplayWebSocketBridgeIo {
    fn new(socket: WebSocket, first: BridgeInFrame) -> Self {
        Self {
            socket,
            pending: Some(first),
        }
    }
}

#[async_trait]
impl BridgeIo for ReplayWebSocketBridgeIo {
    async fn recv(&mut self) -> Option<BridgeInFrame> {
        if let Some(f) = self.pending.take() {
            return Some(f);
        }
        loop {
            match self.socket.recv().await {
                Some(Ok(Message::Text(t))) => return Some(BridgeInFrame::Text(t)),
                Some(Ok(Message::Binary(b))) => return Some(BridgeInFrame::Binary(b)),
                Some(Ok(Message::Close(_))) => return Some(BridgeInFrame::ClosedByClient),
                Some(Ok(Message::Ping(_))) | Some(Ok(Message::Pong(_))) => continue,
                Some(Err(e)) => {
                    debug!(target: "voice", err = %e, "websocket recv errored; treating as close");
                    return None;
                }
                None => return None,
            }
        }
    }

    async fn send(&mut self, frame: BridgeOutFrame) -> Result<(), BridgeIoError> {
        let msg = match frame {
            BridgeOutFrame::Text(t) => Message::Text(t),
            BridgeOutFrame::Binary(b) => Message::Binary(b),
            BridgeOutFrame::Close { code, reason } => Message::Close(Some(CloseFrame {
                code,
                reason: reason.into(),
            })),
        };
        self.socket.send(msg).await.map_err(|_| BridgeIoError)
    }
}

/// Build the canonical `503 voice_disabled` response.
///
/// Pulled out of the handler so iter 2's upgrade path can short-circuit
/// to the same body when subprotocol negotiation rejects the request
/// before the upgrade succeeds.
fn voice_disabled_response() -> Response {
    let mut headers = HeaderMap::new();
    // 24h — the alpha cost gate is opt-in at the operator level; if
    // monitors poll voice they should poll roughly daily, not at the
    // default 5s healthcheck cadence.
    headers.insert(header::RETRY_AFTER, HeaderValue::from_static("86400"));
    let body = Json(json!({
        "error": "voice_disabled",
        "message": "the [voice] feature flag is off; set [voice] enabled = true \
                    in config.toml to enable the alpha",
        "doc": "docs/design/phase4-w4-d4-design.md",
    }));
    (StatusCode::SERVICE_UNAVAILABLE, headers, body).into_response()
}

/// Pre-upgrade rejection for an unsupported / missing subprotocol.
/// The design's "1002 protocol error" close code only applies after a
/// successful upgrade; refusing the upgrade itself uses HTTP 400 with
/// a JSON body so non-WebSocket probes still get a useful error.
fn subprotocol_rejected_response(reason: &str) -> Response {
    let body = Json(json!({
        "error": "subprotocol_rejected",
        "message": reason,
        "expected_subprotocol": framing::SUBPROTOCOL,
    }));
    (StatusCode::BAD_REQUEST, body).into_response()
}

/// 429 Too Many Requests body for the budget-gate refusal. Carries
/// `reset_at` (UNIX seconds, next UTC midnight) so the client can
/// schedule a retry at the right time without polling.
fn budget_exhausted_response(reason: &BudgetDenyReason, reset_at: u64) -> Response {
    let (code, message) = match reason {
        BudgetDenyReason::DayBudgetExhausted {
            used_seconds,
            cap_seconds,
        } => (
            "budget_exhausted",
            format!(
                "tenant has used {}s of the {}s daily voice budget",
                used_seconds, cap_seconds
            ),
        ),
        BudgetDenyReason::BudgetIsZero => (
            "budget_exhausted",
            "voice.budget_minutes_per_tenant_per_day is set to 0; \
             voice is administratively disabled for this tenant"
                .to_string(),
        ),
    };
    let body = Json(json!({
        "error": code,
        "message": message,
        "reset_at": reset_at,
    }));
    (StatusCode::TOO_MANY_REQUESTS, body).into_response()
}

/// Returned when the request looked correct on the surface (flag on +
/// subprotocol matched) but isn't a WebSocket upgrade — typically a
/// curl probe or a misconfigured client. RFC 7231 §6.5.15.
fn upgrade_required_response() -> Response {
    let body = Json(json!({
        "error": "upgrade_required",
        "message": "send `Upgrade: websocket` + `Connection: Upgrade` headers \
                    plus Sec-WebSocket-Version: 13 to open a voice session",
    }));
    (StatusCode::UPGRADE_REQUIRED, body).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use arc_swap::ArcSwap;
    use axum::body::to_bytes;
    use axum::http::Request;
    use corlinman_core::config::VoiceConfig;
    use std::sync::Arc;
    use tower::util::ServiceExt;

    fn router_for(cfg: Config) -> Router {
        let state = VoiceState::new(Arc::new(ArcSwap::from_pointee(cfg)));
        router_with_state(state)
    }

    fn get_voice() -> Request<axum::body::Body> {
        Request::builder()
            .method("GET")
            .uri("/voice")
            .body(axum::body::Body::empty())
            .unwrap()
    }

    #[tokio::test]
    async fn voice_disabled_returns_503() {
        let cfg = Config::default();
        assert!(cfg.voice.is_none(), "default config has no [voice] section");
        let resp = router_for(cfg).oneshot(get_voice()).await.unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(
            resp.headers()
                .get(header::RETRY_AFTER)
                .and_then(|v| v.to_str().ok()),
            Some("86400"),
            "503 must include Retry-After: 86400 to keep monitors from hammering"
        );
        let body = to_bytes(resp.into_body(), 64 * 1024).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["error"], "voice_disabled");
    }

    #[tokio::test]
    async fn voice_disabled_when_section_present_but_flag_off() {
        // Operator may keep the section around with `enabled = false`
        // for reference; the route still 503s.
        let cfg = Config {
            voice: Some(VoiceConfig {
                enabled: false,
                ..VoiceConfig::default()
            }),
            ..Default::default()
        };
        let resp = router_for(cfg).oneshot(get_voice()).await.unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn voice_enabled_plain_get_misses_subprotocol_400s() {
        // Once enabled, the route is a WebSocket endpoint. A plain GET
        // without subprotocol header is rejected first by the
        // negotiation step → 400 subprotocol_rejected. Iter-1's 501
        // stub is gone.
        let cfg = Config {
            voice: Some(VoiceConfig {
                enabled: true,
                ..VoiceConfig::default()
            }),
            ..Default::default()
        };
        let resp = router_for(cfg).oneshot(get_voice()).await.unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let body = to_bytes(resp.into_body(), 64 * 1024).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["error"], "subprotocol_rejected");
    }

    #[tokio::test]
    async fn voice_enabled_subprotocol_ok_but_no_upgrade_426s() {
        // Subprotocol matched but no `Upgrade: websocket` headers →
        // 426 Upgrade Required (RFC 7231 §6.5.15).
        let cfg = Config {
            voice: Some(VoiceConfig {
                enabled: true,
                ..VoiceConfig::default()
            }),
            ..Default::default()
        };
        let req = Request::builder()
            .method("GET")
            .uri("/voice")
            .header(http::header::SEC_WEBSOCKET_PROTOCOL, "corlinman.voice.v1")
            .body(axum::body::Body::empty())
            .unwrap();
        let resp = router_for(cfg).oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::UPGRADE_REQUIRED);
    }

    #[tokio::test]
    async fn stub_router_always_503s() {
        // The legacy stub composition (no live config) always 503s —
        // production callers must use router_with_state to enable voice.
        let resp = router().oneshot(get_voice()).await.unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn voice_flag_hot_reloads_disabled_to_enabled() {
        // Flipping the live ArcSwap'd config must change the next
        // request's response without rebuilding the router. Mirrors
        // the existing config-watcher contract for other routes.
        // Disabled -> 503; enabled-but-no-upgrade-hdrs -> 400.
        let cfg = Config::default();
        let arcs = Arc::new(ArcSwap::from_pointee(cfg));
        let state = VoiceState::new(arcs.clone());
        let app = router_with_state(state);

        // First call: disabled.
        let resp = app.clone().oneshot(get_voice()).await.unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);

        // Hot-flip enabled.
        let next = Config {
            voice: Some(VoiceConfig {
                enabled: true,
                ..VoiceConfig::default()
            }),
            ..Default::default()
        };
        arcs.store(Arc::new(next));

        let resp = app.oneshot(get_voice()).await.unwrap();
        // No subprotocol on a plain GET → 400 subprotocol_rejected.
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    // ----- iter 2: WebSocket upgrade + subprotocol negotiation -----

    fn ws_upgrade_request(subprotocol: Option<&str>) -> Request<axum::body::Body> {
        let mut builder = Request::builder()
            .method("GET")
            .uri("/voice")
            .header(http::header::CONNECTION, "Upgrade")
            .header(http::header::UPGRADE, "websocket")
            .header(http::header::SEC_WEBSOCKET_VERSION, "13")
            // arbitrary 16-byte base64 key
            .header(http::header::SEC_WEBSOCKET_KEY, "dGhlIHNhbXBsZSBub25jZQ==");
        if let Some(p) = subprotocol {
            builder = builder.header(http::header::SEC_WEBSOCKET_PROTOCOL, p);
        }
        builder.body(axum::body::Body::empty()).unwrap()
    }

    fn enabled_voice_router() -> Router {
        let cfg = Config {
            voice: Some(VoiceConfig {
                enabled: true,
                ..VoiceConfig::default()
            }),
            ..Default::default()
        };
        router_for(cfg)
    }

    #[tokio::test]
    async fn voice_upgrade_with_correct_subprotocol_passes_negotiation() {
        // Real WebSocket upgrade → 101 Switching Protocols requires a
        // live TCP connection (hyper attaches the `OnUpgrade` extension
        // there). `tower::oneshot` doesn't, so the extractor falls
        // through to our `upgrade_required_response()` → 426. The
        // important contract this test pins is **"the request reached
        // the upgrade attempt instead of being rejected by subprotocol
        // negotiation"**: 426 not 400 means negotiation succeeded.
        // The full 101 handshake is exercised by the integration test
        // that lands in iter 4+ when the bridge code arrives.
        let resp = enabled_voice_router()
            .oneshot(ws_upgrade_request(Some("corlinman.voice.v1")))
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::UPGRADE_REQUIRED,
            "negotiation passed; oneshot harness can't supply OnUpgrade"
        );
    }

    #[tokio::test]
    async fn voice_upgrade_with_wrong_subprotocol_400s() {
        let resp = enabled_voice_router()
            .oneshot(ws_upgrade_request(Some("corlinman.voice.v0")))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let body = to_bytes(resp.into_body(), 64 * 1024).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["error"], "subprotocol_rejected");
        assert_eq!(v["expected_subprotocol"], "corlinman.voice.v1");
    }

    #[tokio::test]
    async fn voice_upgrade_without_subprotocol_400s() {
        // Design contract: no subprotocol header = ambiguous = refuse.
        let resp = enabled_voice_router()
            .oneshot(ws_upgrade_request(None))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn voice_upgrade_with_voice_disabled_503s_even_with_subprotocol() {
        // The flag check runs before subprotocol negotiation — a
        // forgotten config flip must not become an upgrade leak.
        let cfg = Config::default(); // voice = None
        let resp = router_for(cfg)
            .oneshot(ws_upgrade_request(Some("corlinman.voice.v1")))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    // ----- iter 3: cost gate -----

    fn router_for_with_spend(cfg: Config, spend: Arc<dyn cost::VoiceSpend>) -> Router {
        let state = VoiceState::with_spend(Arc::new(ArcSwap::from_pointee(cfg)), spend);
        router_with_state(state)
    }

    fn enabled_cfg(budget_min: u32) -> Config {
        Config {
            voice: Some(VoiceConfig {
                enabled: true,
                budget_minutes_per_tenant_per_day: budget_min,
                ..VoiceConfig::default()
            }),
            ..Default::default()
        }
    }

    #[tokio::test]
    async fn budget_check_allows_when_under_cap() {
        // 30 min/day cap, fresh spend store → upgrade reaches the
        // negotiation-success path (which oneshot can't fully drive,
        // so we land on 426 / Upgrade Required). The important
        // contract: 429 was NOT returned.
        let resp = router_for(enabled_cfg(30))
            .oneshot(ws_upgrade_request(Some("corlinman.voice.v1")))
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::UPGRADE_REQUIRED,
            "should reach upgrade attempt, not 429"
        );
    }

    #[tokio::test]
    async fn budget_check_refuses_when_at_cap() {
        // Pre-populate the spend store so today's seconds_used >= cap.
        let spend: Arc<dyn cost::VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let day = utc_day_epoch(now_unix_secs());
        spend.add_seconds("default", day, 30 * 60);

        let resp = router_for_with_spend(enabled_cfg(30), spend)
            .oneshot(ws_upgrade_request(Some("corlinman.voice.v1")))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::TOO_MANY_REQUESTS);
        let body = to_bytes(resp.into_body(), 64 * 1024).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["error"], "budget_exhausted");
        assert!(v["reset_at"].is_u64());
    }

    #[tokio::test]
    async fn budget_check_refuses_when_cap_is_zero() {
        // Operator zeroed the cap as a kill-switch.
        let resp = router_for(enabled_cfg(0))
            .oneshot(ws_upgrade_request(Some("corlinman.voice.v1")))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::TOO_MANY_REQUESTS);
    }

    #[tokio::test]
    async fn budget_check_isolates_per_tenant() {
        // Tenant A has burned the cap; tenant B is fresh — only A
        // gets refused.
        let spend: Arc<dyn cost::VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let day = utc_day_epoch(now_unix_secs());
        spend.add_seconds("a", day, 30 * 60);

        // Build the router once and reuse — both tenants hit the same
        // shared spend store via the X-Tenant-Id header.
        let state = VoiceState::with_spend(Arc::new(ArcSwap::from_pointee(enabled_cfg(30))), spend);
        let router = router_with_state(state);

        let mut req_a = ws_upgrade_request(Some("corlinman.voice.v1"));
        req_a
            .headers_mut()
            .insert("x-tenant-id", "a".parse().unwrap());
        let resp_a = router.clone().oneshot(req_a).await.unwrap();
        assert_eq!(resp_a.status(), StatusCode::TOO_MANY_REQUESTS);

        let mut req_b = ws_upgrade_request(Some("corlinman.voice.v1"));
        req_b
            .headers_mut()
            .insert("x-tenant-id", "b".parse().unwrap());
        let resp_b = router.oneshot(req_b).await.unwrap();
        assert_eq!(resp_b.status(), StatusCode::UPGRADE_REQUIRED);
    }

    #[tokio::test]
    async fn budget_check_runs_after_subprotocol_check() {
        // Wrong subprotocol must still 400 even when budget is at cap.
        // (Don't burn an HTTP-cycle telling the operator about budget
        // when the request couldn't have completed anyway.)
        let spend: Arc<dyn cost::VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let day = utc_day_epoch(now_unix_secs());
        spend.add_seconds("default", day, 30 * 60);

        let resp = router_for_with_spend(enabled_cfg(30), spend)
            .oneshot(ws_upgrade_request(Some("corlinman.voice.v0")))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    // ----- iter 6: persistence state seams -----

    #[tokio::test]
    async fn state_seam_carries_session_store_and_transcript_sink() {
        // Iter 6 contract: VoiceState exposes builder methods that
        // wire a session store + transcript sink without breaking
        // existing call sites. The route handler doesn't read these
        // yet (iter 7+ wires the upgrade-path), but the seam shape
        // is pinned here so a refactor doesn't silently regress.
        use persistence::{
            MemoryTranscriptSink, SqliteVoiceSessionStore, VoiceSessionStart, VoiceSessionStore,
            VoiceTranscriptSink,
        };

        let cfg = Config::default();
        let store = Arc::new(
            SqliteVoiceSessionStore::open_in_memory()
                .await
                .expect("in-memory store opens"),
        );
        let sink = Arc::new(MemoryTranscriptSink::new());

        let state = VoiceState::new(Arc::new(ArcSwap::from_pointee(cfg)))
            .with_session_store(store.clone() as Arc<dyn VoiceSessionStore>)
            .with_transcript_sink(sink.clone() as Arc<dyn VoiceTranscriptSink>);
        assert!(state.session_store.is_some());
        assert!(state.transcript_sink.is_some());

        // The store seam is genuinely usable through the trait — write
        // and read a row to prove it.
        store
            .record_start(&VoiceSessionStart {
                id: "voice-state-seam".into(),
                tenant_id: "default".into(),
                session_key: "k".into(),
                agent_id: None,
                provider_alias: "openai-realtime".into(),
                started_at: 42,
            })
            .await
            .expect("record_start through trait");
        let row = store
            .fetch("voice-state-seam")
            .await
            .expect("fetch")
            .expect("row present");
        assert_eq!(row.id, "voice-state-seam");

        // Transcript sink seam likewise functional.
        sink.append_turn("default", "k", "user", "hi")
            .await
            .unwrap();
        assert_eq!(sink.snapshot().await.len(), 1);
    }

    #[tokio::test]
    async fn state_seam_omitted_keeps_handler_path_intact() {
        // Negative companion of the above: a state without store / sink
        // (the iter-3 default) still routes 503 / 400 / 426 the same
        // way — proves the seam additions don't change behaviour for
        // operators who haven't enabled persistence.
        let cfg = Config {
            voice: Some(VoiceConfig {
                enabled: true,
                ..VoiceConfig::default()
            }),
            ..Default::default()
        };
        let resp = router_for(cfg).oneshot(get_voice()).await.unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }
}
