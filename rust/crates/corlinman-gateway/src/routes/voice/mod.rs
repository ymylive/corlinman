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

pub mod framing;

use std::sync::Arc;

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

use framing::{accept_subprotocol, encode_server_control, ServerControl, SubprotocolDecision};

/// State carried by the `/voice` route. We snapshot the live config so
/// flipping `[voice] enabled` at runtime via the config-watcher takes
/// effect without a server restart.
#[derive(Clone)]
pub struct VoiceState {
    pub config: Arc<ArcSwap<Config>>,
}

impl VoiceState {
    pub fn new(config: Arc<ArcSwap<Config>>) -> Self {
        Self { config }
    }
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

    // At this point the flag is on AND the subprotocol matches. The
    // request still has to be a real WebSocket upgrade — a plain GET
    // with the right `Sec-WebSocket-Protocol` header but no `Upgrade:
    // websocket` is a malformed client, not a security event.
    let ws = match ws {
        Some(ws) => ws,
        None => return upgrade_required_response(),
    };

    // Resolve the provider alias *now* so the upgrade response carries
    // a stable identifier into `started`. Falls back to default_voice
    // if the section is unexpectedly None at this point (which would
    // be a bug; the early-return above should have caught it).
    let provider = snap
        .voice
        .as_ref()
        .map(|v| v.provider_alias.clone())
        .unwrap_or_else(|| "openai-realtime".to_string());

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
}
