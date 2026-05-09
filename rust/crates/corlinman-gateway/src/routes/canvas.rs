//! `/canvas/*` — Canvas Host endpoints.
//!
//! Phase 1 (B5-BE1) shipped only the transport / session bookkeeping —
//! `/canvas/session`, `/canvas/frame`, `/canvas/session/:id/events` —
//! and was explicit that "there is no renderer here". Phase 4 W3 C3
//! iter 8 closes that gap by mounting one more route on the same
//! sub-router:
//!
//! - `POST /canvas/session`             — create an in-memory canvas session
//! - `POST /canvas/frame`               — post a frame event to a session
//! - `GET  /canvas/session/:id/events`  — Server-Sent Events stream
//! - `POST /canvas/render`              — **iter 8**: synchronous renderer
//!
//! All four are behind [`crate::middleware::admin_auth::require_admin`]
//! and gated by `[canvas] host_endpoint_enabled`; when the config flag
//! is off every route returns 503 with a structured error.
//!
//! ## Why a separate `/canvas/render` instead of folding into `/frame`
//!
//! `phase4-w3-c3-design.md` § "Implementation order" iter 8 originally
//! sketched in-line enrichment of `present` frames during the SSE
//! fan-out. C3 iter 8 instead lands a dedicated synchronous endpoint
//! because:
//!
//! 1. The renderer is a pure function. Surfacing it as a request /
//!    response makes it independently testable, cacheable at the HTTP
//!    layer, and reusable from non-canvas-session callers (Swift
//!    client preview, CLI, future static export).
//! 2. The Phase-1 SSE machinery stays byte-identical — no new failure
//!    modes around per-event renderer panics, no new latency on the
//!    fan-out path.
//! 3. Producers that want enriched-on-fan-out semantics can issue
//!    `/canvas/render` first, then `/canvas/frame` with the rendered
//!    HTML in the payload — explicit, idempotency-key safe.
//!
//! Folding the renderer back into `/canvas/frame` is a follow-up
//! iteration if profiling shows producer round-trip cost matters.
//!
//! Session state lives in-process in an `Arc<RwLock<HashMap<...>>>`. This is
//! intentionally a stub: B5-BE1 only needs the protocol wire-up so downstream
//! workstreams can build renderer UIs against a stable contract. A background
//! task reaps expired entries every second; the SSE stream also self-closes
//! when it observes expiry so clients don't hang past the TTL.

use std::collections::HashMap;
use std::convert::Infallible;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use arc_swap::ArcSwap;
use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{
        sse::{Event as SseEvent, KeepAlive, Sse},
        IntoResponse, Response,
    },
    routing::{get, post},
    Json, Router,
};
use corlinman_canvas::{CanvasError, CanvasPresentPayload, Renderer};
use corlinman_core::config::Config;
use futures::Stream;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::{broadcast, mpsc, RwLock};
use tokio_stream::wrappers::ReceiverStream;
use tracing::{debug, info, warn};
use uuid::Uuid;

use crate::middleware::admin_auth::{require_admin, AdminAuthState};

// ---------------------------------------------------------------------------
// Iter 8 — renderer constants
// ---------------------------------------------------------------------------

/// Per-artifact body byte cap for `POST /canvas/render`. Mirrors the
/// design's `[canvas] max_artifact_bytes = 262144`. Lives here as a
/// `const` until C3 extends the `[canvas]` config block; switching to
/// a config-driven value is a one-line change in `render_artifact`.
const MAX_ARTIFACT_BYTES: usize = 262_144;

/// Renderer cache capacity. Mirrors the design's
/// `[canvas] cache_max_entries = 512`. Same trajectory as
/// [`MAX_ARTIFACT_BYTES`] — flips to config-driven once the C3
/// `[canvas]` extension lands.
const RENDERER_CACHE_CAPACITY: usize = 512;

/// Whitelist of accepted `kind` values on `POST /canvas/frame`. Anything else
/// → 400 `invalid_frame_kind`.
const ALLOWED_FRAME_KINDS: &[&str] = &[
    "present",
    "hide",
    "navigate",
    "eval",
    "snapshot",
    "a2ui_push",
    "a2ui_reset",
];

/// Broadcast channel capacity for SSE fan-out per session. Small by design —
/// this is a protocol stub, not a high-fan-out bus. Laggy receivers drop.
const SSE_CHANNEL_CAPACITY: usize = 64;

/// In-memory canvas session. Not `Clone` — lives inside the store map and is
/// handed out only as references or via targeted clones of fields.
struct CanvasSession {
    #[allow(dead_code)] // surfaced to the renderer in B5-BE2; retained so the
    // session store is introspectable (e.g. via a future GET).
    title: String,
    #[allow(dead_code)] // stored for future renderer handoff; not served today.
    initial_state: Value,
    expires_at_ms: u64,
    events: Vec<CanvasEvent>,
    subscribers: broadcast::Sender<CanvasEvent>,
    /// Per-session expiry notifier. Cloned to each SSE task so it can
    /// terminate promptly when the janitor reaps the session.
    expired: broadcast::Sender<()>,
}

/// Single frame event flowing through the canvas bus. Cheap to clone — the
/// payload is already `serde_json::Value` behind an `Arc`-less box, which
/// matches the `ApprovalEvent` precedent.
#[derive(Debug, Clone, Serialize)]
pub struct CanvasEvent {
    pub event_id: String,
    pub session_id: String,
    pub kind: String,
    pub payload: Value,
    pub at_ms: u64,
}

/// Shared handle passed to every canvas handler. Cloneable; all heavy state
/// sits behind `Arc`.
#[derive(Clone)]
pub struct CanvasState {
    config: Arc<ArcSwap<Config>>,
    sessions: Arc<RwLock<HashMap<String, CanvasSession>>>,
    /// Iter 8 — content-addressed renderer shared by every
    /// `/canvas/render` request. The crate's adapters lazy-init their
    /// own state (syntect `SyntaxSet`, katex `KatexContext`) behind
    /// `OnceLock`s, so this `Arc<Renderer>` is cheap to clone and the
    /// LRU is shared across handler invocations.
    renderer: Arc<Renderer>,
}

impl CanvasState {
    /// Construct a state handle and spawn the background janitor. The janitor
    /// lives for the process lifetime (detached task).
    pub fn new(config: Arc<ArcSwap<Config>>) -> Self {
        let sessions = Arc::new(RwLock::new(HashMap::new()));
        let janitor_sessions = sessions.clone();
        tokio::spawn(async move {
            janitor_loop(janitor_sessions).await;
        });
        Self {
            config,
            sessions,
            renderer: Arc::new(Renderer::with_cache(RENDERER_CACHE_CAPACITY)),
        }
    }
}

/// Wall-clock millis since the UNIX epoch. Monotonicity is NOT required; we
/// only use this for client-visible timestamps and coarse TTL comparisons.
fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Allocate a session id of the form `cs_` + 8 lowercase hex chars, derived
/// from a fresh UUIDv4 so the ids are reasonably unique across process
/// restarts without bringing in `rand` as a new dep.
fn new_session_id() -> String {
    let u = Uuid::new_v4();
    let bytes = u.as_bytes();
    format!(
        "cs_{:02x}{:02x}{:02x}{:02x}",
        bytes[0], bytes[1], bytes[2], bytes[3]
    )
}

/// Background task that reaps expired sessions roughly once a second. On
/// eviction we fire the per-session `expired` broadcaster so the SSE tasks
/// can send their `event: end` frame and close promptly.
async fn janitor_loop(sessions: Arc<RwLock<HashMap<String, CanvasSession>>>) {
    let mut interval = tokio::time::interval(Duration::from_secs(1));
    // First tick fires immediately; skip it to avoid a needless scan at
    // startup before any session could possibly have expired.
    interval.tick().await;
    loop {
        interval.tick().await;
        let now = now_ms();
        let mut expired_ids: Vec<String> = Vec::new();
        {
            let map = sessions.read().await;
            for (id, sess) in map.iter() {
                if sess.expires_at_ms <= now {
                    expired_ids.push(id.clone());
                }
            }
        }
        if expired_ids.is_empty() {
            continue;
        }
        let mut map = sessions.write().await;
        for id in &expired_ids {
            if let Some(sess) = map.remove(id) {
                // Best-effort notify. Dropped subscribers are fine.
                let _ = sess.expired.send(());
                debug!(session_id = %id, "canvas session expired");
            }
        }
    }
}

/// Build the canvas sub-router, wrapped in the admin auth guard. The four
/// routes share the same [`CanvasState`] + [`AdminAuthState`].
pub fn router(canvas_state: CanvasState, auth_state: AdminAuthState) -> Router {
    Router::new()
        .route("/canvas/session", post(create_session))
        .route("/canvas/frame", post(post_frame))
        .route("/canvas/session/:id/events", get(stream_events))
        // Iter 8 — synchronous renderer. Independent of the session
        // store (caller drives `/canvas/render` followed by an
        // optional `/canvas/frame` if they want SSE fan-out).
        .route("/canvas/render", post(render_artifact))
        .with_state(canvas_state)
        .layer(axum::middleware::from_fn_with_state(
            auth_state,
            require_admin,
        ))
}

// ---------------------------------------------------------------------------
// Config gating
// ---------------------------------------------------------------------------

fn disabled_response() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({
            "error": "canvas_host_disabled",
            "message": "Set [canvas] host_endpoint_enabled = true in config.toml",
        })),
    )
        .into_response()
}

/// `true` when the feature is enabled in the current config snapshot.
fn canvas_enabled(state: &CanvasState) -> bool {
    state.config.load().canvas.host_endpoint_enabled
}

// ---------------------------------------------------------------------------
// POST /canvas/session
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct CreateSessionBody {
    #[serde(default)]
    title: Option<String>,
    #[serde(default)]
    initial_state: Option<Value>,
    #[serde(default)]
    ttl_secs: Option<u64>,
}

#[derive(Debug, Serialize)]
struct CreateSessionOut {
    session_id: String,
    created_at_ms: u64,
    expires_at_ms: u64,
}

async fn create_session(
    State(state): State<CanvasState>,
    body: Option<Json<CreateSessionBody>>,
) -> Response {
    if !canvas_enabled(&state) {
        return disabled_response();
    }

    let Json(body) = body.unwrap_or(Json(CreateSessionBody {
        title: None,
        initial_state: None,
        ttl_secs: None,
    }));

    // TTL precedence: explicit body > config default. Clamped to the
    // config's validator range (1 .. 86_400).
    let cfg_ttl = state.config.load().canvas.session_ttl_secs as u64;
    let ttl_secs = body.ttl_secs.unwrap_or(cfg_ttl).clamp(1, 86_400);

    let created_at_ms = now_ms();
    let expires_at_ms = created_at_ms + ttl_secs * 1000;
    let title = body.title.unwrap_or_else(|| "untitled".to_string());
    let initial_state = body.initial_state.unwrap_or_else(|| json!({}));

    let (tx, _rx) = broadcast::channel::<CanvasEvent>(SSE_CHANNEL_CAPACITY);
    let (expired_tx, _) = broadcast::channel::<()>(1);
    let session_id = new_session_id();

    let session = CanvasSession {
        title: title.clone(),
        initial_state,
        expires_at_ms,
        events: Vec::new(),
        subscribers: tx,
        expired: expired_tx,
    };
    state
        .sessions
        .write()
        .await
        .insert(session_id.clone(), session);

    info!(
        %session_id,
        %title,
        ttl_secs,
        "canvas session created",
    );

    (
        StatusCode::CREATED,
        Json(CreateSessionOut {
            session_id,
            created_at_ms,
            expires_at_ms,
        }),
    )
        .into_response()
}

// ---------------------------------------------------------------------------
// POST /canvas/frame
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct PostFrameBody {
    session_id: String,
    kind: String,
    #[serde(default)]
    payload: Value,
}

async fn post_frame(State(state): State<CanvasState>, Json(body): Json<PostFrameBody>) -> Response {
    if !canvas_enabled(&state) {
        return disabled_response();
    }

    if !ALLOWED_FRAME_KINDS.contains(&body.kind.as_str()) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({
                "error": "invalid_frame_kind",
                "message": format!("kind '{}' is not in the whitelist", body.kind),
                "allowed": ALLOWED_FRAME_KINDS,
            })),
        )
            .into_response();
    }

    let event = CanvasEvent {
        event_id: Uuid::new_v4().to_string(),
        session_id: body.session_id.clone(),
        kind: body.kind,
        payload: body.payload,
        at_ms: now_ms(),
    };

    let mut sessions = state.sessions.write().await;
    let Some(session) = sessions.get_mut(&body.session_id) else {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({
                "error": "session_not_found",
                "session_id": body.session_id,
            })),
        )
            .into_response();
    };
    if session.expires_at_ms <= now_ms() {
        // Expired but the janitor hasn't run yet; treat as not-found to
        // match the post-GC contract.
        return (
            StatusCode::NOT_FOUND,
            Json(json!({
                "error": "session_not_found",
                "session_id": body.session_id,
            })),
        )
            .into_response();
    }

    session.events.push(event.clone());
    // Fan out. `send` errors only when there are no receivers; not an error.
    let _ = session.subscribers.send(event.clone());

    (
        StatusCode::ACCEPTED,
        Json(json!({
            "event_id": event.event_id,
        })),
    )
        .into_response()
}

// ---------------------------------------------------------------------------
// GET /canvas/session/:id/events  (SSE)
// ---------------------------------------------------------------------------

async fn stream_events(State(state): State<CanvasState>, Path(id): Path<String>) -> Response {
    if !canvas_enabled(&state) {
        return disabled_response();
    }

    let (event_rx, expired_rx) = {
        let map = state.sessions.read().await;
        let Some(session) = map.get(&id) else {
            return (
                StatusCode::NOT_FOUND,
                Json(json!({
                    "error": "session_not_found",
                    "session_id": id,
                })),
            )
                .into_response();
        };
        (session.subscribers.subscribe(), session.expired.subscribe())
    };

    let session_id = id;
    let stream = build_sse_stream(event_rx, expired_rx, session_id);

    Sse::new(stream)
        .keep_alive(KeepAlive::default())
        .into_response()
}

/// Merge the per-session event bus and the expiry notifier into one SSE
/// stream. Emits `event: canvas` frames until either the janitor signals
/// expiry (→ send `event: end` and close) or the session's broadcaster is
/// dropped (same outcome).
///
/// Implemented as a spawned task that fans both receivers into a single
/// `mpsc`. `ReceiverStream` turns that into a `futures::Stream` we can hand
/// to `Sse::new`. Using `tokio::select!` avoids depending on `async-stream`.
fn build_sse_stream(
    mut event_rx: broadcast::Receiver<CanvasEvent>,
    mut expired_rx: broadcast::Receiver<()>,
    session_id: String,
) -> impl Stream<Item = Result<SseEvent, Infallible>> {
    let (tx, rx) = mpsc::channel::<Result<SseEvent, Infallible>>(SSE_CHANNEL_CAPACITY);
    tokio::spawn(async move {
        loop {
            tokio::select! {
                biased;
                expired = expired_rx.recv() => {
                    if expired.is_ok() {
                        let _ = tx.send(Ok(
                            SseEvent::default().event("end").data("\"expired\""),
                        )).await;
                    }
                    // Whether we got the marker or the sender was dropped
                    // (treated as implicit expiry), we terminate the stream.
                    break;
                }
                recv = event_rx.recv() => {
                    match recv {
                        Ok(evt) => {
                            let body = serde_json::to_string(&evt)
                                .unwrap_or_else(|_| "{}".to_string());
                            let frame = SseEvent::default().event("canvas").data(body);
                            if tx.send(Ok(frame)).await.is_err() {
                                break; // client disconnected
                            }
                        }
                        Err(broadcast::error::RecvError::Lagged(n)) => {
                            warn!(
                                session_id = %session_id,
                                lagged = n,
                                "canvas sse subscriber lagged",
                            );
                            let frame = SseEvent::default()
                                .event("lag")
                                .data(format!("lagged {n}"));
                            if tx.send(Ok(frame)).await.is_err() {
                                break;
                            }
                        }
                        Err(broadcast::error::RecvError::Closed) => {
                            // Session dropped without a janitor tick — emit
                            // an `end` for symmetry and close.
                            let _ = tx.send(Ok(
                                SseEvent::default().event("end").data("\"expired\""),
                            )).await;
                            break;
                        }
                    }
                }
            }
        }
    });
    ReceiverStream::new(rx)
}

// ---------------------------------------------------------------------------
// POST /canvas/render — Phase 4 W3 C3 iter 8
// ---------------------------------------------------------------------------

/// Synchronously render a `present`-frame payload to HTML.
///
/// Wire shape mirrors `phase4-w3-c3-design.md` § "Protocol surface":
///
/// ```json
/// // request
/// {
///   "artifact_kind": "code",
///   "body": { "language": "rust", "source": "fn main(){}" },
///   "idempotency_key": "art_a1b2",
///   "theme_hint": "tp-light"
/// }
///
/// // 200 response
/// {
///   "html_fragment": "<pre class=\"cn-canvas-code\">…</pre>",
///   "theme_class": "tp-light",
///   "content_hash": "<64-char hex>",
///   "render_kind": "code",
///   "warnings": []
/// }
/// ```
///
/// Failure modes:
/// - 503 `canvas_host_disabled` — `[canvas] host_endpoint_enabled = false`.
/// - 400 `invalid_payload`      — JSON didn't match `CanvasPresentPayload`
///   (unknown artifact_kind, missing fields, mismatched body shape).
/// - 413 `body_too_large`       — total body bytes exceed `MAX_ARTIFACT_BYTES`.
/// - 422 `render_failed`        — adapter / mermaid timeout / oversize SVG /
///   adapter-specific error. Body carries `code` (`Timeout`,
///   `BodyTooLarge`, `Adapter`, `UnknownKind`) so the UI fallback panel
///   knows which lucide icon to show.
///
/// Authn / authz: ride the existing `require_admin` layer mounted by
/// [`router`]. No additional checks here — render itself is a pure
/// function of its input.
async fn render_artifact(State(state): State<CanvasState>, body: axum::body::Bytes) -> Response {
    if !canvas_enabled(&state) {
        return disabled_response();
    }

    if body.len() > MAX_ARTIFACT_BYTES {
        return (
            StatusCode::PAYLOAD_TOO_LARGE,
            Json(json!({
                "error": "body_too_large",
                "max_bytes": MAX_ARTIFACT_BYTES,
                "actual_bytes": body.len(),
            })),
        )
            .into_response();
    }

    let payload: CanvasPresentPayload = match serde_json::from_slice(&body) {
        Ok(p) => p,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({
                    "error": "invalid_payload",
                    "message": e.to_string(),
                })),
            )
                .into_response();
        }
    };

    match state.renderer.render(&payload) {
        Ok(art) => (StatusCode::OK, Json(art)).into_response(),
        Err(err) => render_error_response(err),
    }
}

/// Map a [`CanvasError`] from the renderer into a structured 4xx
/// response. The UI's `canvas-artifact-error` component (iter 9) keys
/// off `code` to pick its lucide icon and messaging.
fn render_error_response(err: CanvasError) -> Response {
    let (status, code, kind) = match &err {
        CanvasError::Unimplemented { kind } => (StatusCode::BAD_REQUEST, "unimplemented", Some(kind)),
        CanvasError::UnknownKind(_) => (StatusCode::BAD_REQUEST, "unknown_kind", None),
        CanvasError::BodyTooLarge { kind, .. } => {
            (StatusCode::UNPROCESSABLE_ENTITY, "body_too_large", Some(kind))
        }
        CanvasError::Timeout { kind, .. } => {
            (StatusCode::UNPROCESSABLE_ENTITY, "timeout", Some(kind))
        }
        CanvasError::Adapter { kind, .. } => {
            (StatusCode::UNPROCESSABLE_ENTITY, "adapter_error", Some(kind))
        }
    };
    let mut body = json!({
        "error": "render_failed",
        "code": code,
        "message": err.to_string(),
    });
    if let Some(kind) = kind {
        body["artifact_kind"] = json!(kind.as_str());
    }
    (status, Json(body)).into_response()
}

// ---------------------------------------------------------------------------
// Tests — unit-level only. Integration tests live in
// `tests/canvas_host.rs` and drive the full HTTP stack.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn session_id_shape() {
        let id = new_session_id();
        assert!(id.starts_with("cs_"));
        assert_eq!(id.len(), 3 + 8);
        assert!(id[3..].chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn frame_kind_whitelist_is_exhaustive() {
        for k in [
            "present",
            "hide",
            "navigate",
            "eval",
            "snapshot",
            "a2ui_push",
            "a2ui_reset",
        ] {
            assert!(ALLOWED_FRAME_KINDS.contains(&k));
        }
        assert!(!ALLOWED_FRAME_KINDS.contains(&"bogus"));
    }
}
