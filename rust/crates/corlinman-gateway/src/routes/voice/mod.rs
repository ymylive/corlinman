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

pub mod cost;
pub mod framing;
pub mod provider;

use std::sync::Arc;
use std::time::SystemTime;

use arc_swap::ArcSwap;
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
use tracing::{debug, warn};

use cost::{
    evaluate_budget, next_utc_midnight, utc_day_epoch, BudgetDecision, BudgetDenyReason,
    InMemoryVoiceSpend, VoiceSpend,
};
use framing::{accept_subprotocol, encode_server_control, ServerControl, SubprotocolDecision};
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
}

impl VoiceState {
    pub fn new(config: Arc<ArcSwap<Config>>) -> Self {
        Self {
            config,
            spend: Arc::new(InMemoryVoiceSpend::new()),
            provider: None,
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
        }
    }

    /// Wire a provider adapter in. Iter 4 ships the trait + mock; iter
    /// 5 wires the real OpenAI Realtime adapter; iter 6+ uses this
    /// seam to drive transcript persistence + audio retention.
    pub fn with_provider(mut self, provider: SharedVoiceProvider) -> Self {
        self.provider = Some(provider);
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
    let enabled = snap
        .voice
        .as_ref()
        .map(|v| v.enabled)
        .unwrap_or(false);

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

    let provider = voice_cfg.provider_alias.clone();
    ws.protocols([accepted])
        .on_upgrade(move |socket| run_voice_session(socket, provider))
}

/// Iter-2 stub upgrade-handler body. Sends a single `started` event
/// then closes the socket with WebSocket close code 1000 (normal).
///
/// Iter 4+ replaces this with the real bridge: spawn the provider
/// WebSocket task, the client-pump task, and the control plane. The
/// shape stays the same so the upgrade handler above is stable across
/// later iters.
async fn run_voice_session(mut socket: WebSocket, provider_alias: String) {
    // Mint a transient session id so even iter-2 round-trips are
    // distinguishable in logs. Iter 3+ promotes this to the row id.
    let session_id = format!("voice-{}", uuid::Uuid::new_v4());
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
        reason: "iter-2 stub: session not yet wired".into(),
    };
    let _ = socket.send(Message::Close(Some(close))).await;
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
        let mut cfg = Config::default();
        cfg.voice = Some(VoiceConfig {
            enabled: false,
            ..VoiceConfig::default()
        });
        let resp = router_for(cfg).oneshot(get_voice()).await.unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn voice_enabled_plain_get_misses_subprotocol_400s() {
        // Once enabled, the route is a WebSocket endpoint. A plain GET
        // without subprotocol header is rejected first by the
        // negotiation step → 400 subprotocol_rejected. Iter-1's 501
        // stub is gone.
        let mut cfg = Config::default();
        cfg.voice = Some(VoiceConfig {
            enabled: true,
            ..VoiceConfig::default()
        });
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
        let mut cfg = Config::default();
        cfg.voice = Some(VoiceConfig {
            enabled: true,
            ..VoiceConfig::default()
        });
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
        let mut next = Config::default();
        next.voice = Some(VoiceConfig {
            enabled: true,
            ..VoiceConfig::default()
        });
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
        let mut cfg = Config::default();
        cfg.voice = Some(VoiceConfig {
            enabled: true,
            ..VoiceConfig::default()
        });
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
        let mut cfg = Config::default();
        cfg.voice = Some(VoiceConfig {
            enabled: true,
            budget_minutes_per_tenant_per_day: budget_min,
            ..VoiceConfig::default()
        });
        cfg
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
        let state = VoiceState::with_spend(
            Arc::new(ArcSwap::from_pointee(enabled_cfg(30))),
            spend,
        );
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
}
