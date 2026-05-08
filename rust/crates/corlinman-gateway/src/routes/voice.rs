//! `GET /voice` — realtime audio WebSocket (Phase 4 W4 D4 alpha).
//!
//! D4 lands the first full-duplex chat surface. The route is **gated** —
//! every minute on the wire bills cents not micro-cents, so:
//!
//!   1. `[voice] enabled = false` (default) → `503 voice_disabled` with
//!      `Retry-After: 86400` so monitors don't hammer.
//!   2. iter 2 will wire the WebSocket upgrade + `corlinman.voice.v1`
//!      subprotocol negotiation.
//!   3. iter 3 will wire the per-tenant cost gate (budget check at
//!      session start, hard-kill at `max_session_seconds`).
//!   4. iter 4+ will wire the actual provider WebSocket bridging.
//!
//! Iter 1 — this file — only ships the stub route + the disabled-503
//! response. Calling the route with `enabled = true` returns 501
//! (`not_implemented_yet`) until iter 2 lands the upgrade handler.
//!
//! See `docs/design/phase4-w4-d4-design.md`.

use std::sync::Arc;

use arc_swap::ArcSwap;
use axum::{
    extract::State,
    http::{header, HeaderMap, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    routing::get,
    Json, Router,
};
use corlinman_core::config::Config;
use serde_json::json;

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

async fn voice_handler(State(state): State<VoiceState>, _headers: HeaderMap) -> Response {
    let snap = state.config.load();
    let enabled = snap
        .voice
        .as_ref()
        .map(|v| v.enabled)
        .unwrap_or(false);

    if !enabled {
        return voice_disabled_response();
    }

    // Iter 1 ships only the gating layer; iter 2 wires the WebSocket
    // upgrade. Until then `enabled = true` resolves to a transparent
    // 501 — the operator who flipped the flag knows the alpha is
    // partially landed and can read the iter-2 release notes.
    not_implemented_yet_response()
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

fn not_implemented_yet_response() -> Response {
    let body = Json(json!({
        "error": "not_implemented_yet",
        "message": "voice route is enabled but the WebSocket upgrade lands in iter 2",
        "iter": 1,
    }));
    (StatusCode::NOT_IMPLEMENTED, body).into_response()
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
    async fn voice_enabled_returns_501_until_iter_2() {
        // Iter-1 contract: enabling the flag exposes the route, but the
        // upgrade handler itself is still a stub.
        let mut cfg = Config::default();
        cfg.voice = Some(VoiceConfig {
            enabled: true,
            ..VoiceConfig::default()
        });
        let resp = router_for(cfg).oneshot(get_voice()).await.unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_IMPLEMENTED);
        let body = to_bytes(resp.into_body(), 64 * 1024).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["error"], "not_implemented_yet");
    }

    #[tokio::test]
    async fn stub_router_always_503s() {
        // The legacy stub composition (no live config) always 503s —
        // production callers must use router_with_state to enable voice.
        let resp = router().oneshot(get_voice()).await.unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn voice_flag_hot_reloads() {
        // Flipping the live ArcSwap'd config must change the next
        // request's response without rebuilding the router. Mirrors
        // the existing config-watcher contract for other routes.
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
        assert_eq!(resp.status(), StatusCode::NOT_IMPLEMENTED);
    }
}
