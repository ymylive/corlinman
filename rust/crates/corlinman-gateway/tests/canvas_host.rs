//! B5-BE1 — Canvas Host endpoint integration tests.
//!
//! All tests boot a real `axum::serve` on `127.0.0.1:0` and talk HTTP via
//! `reqwest`. SSE tests consume the raw `Content-Type: text/event-stream`
//! body as a chunked byte stream because we need ordering + timing
//! guarantees that `tower::ServiceExt::oneshot` can't give us for
//! long-lived responses.
//!
//! The tests share a small harness (`spawn_gateway`) that:
//!   * builds a minimal admin config with known Basic-auth credentials,
//!   * constructs a `CanvasState` + `AdminAuthState`,
//!   * merges them into an axum Router,
//!   * binds a random local port and spawns `axum::serve` with a graceful
//!     shutdown handle so the test can tear it down at the end.
//!
//! Keeping the harness inline (no shared test-utils crate) mirrors the
//! pattern used by `approval_gate_e2e.rs` and `chat_plugin_e2e.rs`.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use arc_swap::ArcSwap;
use axum::Router;
use base64::Engine;
use corlinman_core::config::Config;
use corlinman_gateway::middleware::admin_auth::AdminAuthState;
use corlinman_gateway::routes::canvas::{self, CanvasState};
use futures::StreamExt;
use serde_json::{json, Value};
use tokio::net::TcpListener;
use tokio::sync::oneshot;

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

const ADMIN_USER: &str = "admin";
const ADMIN_PASS: &str = "secret";

fn hash_password(password: &str) -> String {
    use argon2::password_hash::{PasswordHasher, SaltString};
    let salt = SaltString::encode_b64(b"corlinman_test_salt_bytes_16").unwrap();
    argon2::Argon2::default()
        .hash_password(password.as_bytes(), &salt)
        .unwrap()
        .to_string()
}

fn basic_auth_header() -> String {
    format!(
        "Basic {}",
        base64::engine::general_purpose::STANDARD.encode(format!("{ADMIN_USER}:{ADMIN_PASS}"))
    )
}

/// Build a `Config` with admin credentials set and the canvas endpoint
/// toggled via `enabled`.
fn make_config(enabled: bool) -> Config {
    let mut cfg = Config::default();
    cfg.admin.username = Some(ADMIN_USER.into());
    cfg.admin.password_hash = Some(hash_password(ADMIN_PASS));
    cfg.canvas.host_endpoint_enabled = enabled;
    // Default TTL stays at 1800; individual tests override via body when
    // they need a shorter horizon.
    cfg
}

struct Gateway {
    addr: SocketAddr,
    shutdown: Option<oneshot::Sender<()>>,
    handle: Option<tokio::task::JoinHandle<()>>,
}

impl Gateway {
    fn url(&self, path: &str) -> String {
        format!("http://{}{path}", self.addr)
    }

    async fn shutdown(mut self) {
        if let Some(tx) = self.shutdown.take() {
            let _ = tx.send(());
        }
        if let Some(h) = self.handle.take() {
            let _ = h.await;
        }
    }
}

async fn spawn_gateway(config: Config) -> Gateway {
    let config_handle = Arc::new(ArcSwap::from_pointee(config));
    let canvas_state = CanvasState::new(config_handle.clone());
    let auth_state = AdminAuthState::new(config_handle);
    let router: Router = canvas::router(canvas_state, auth_state);

    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let (tx, rx) = oneshot::channel::<()>();
    let handle = tokio::spawn(async move {
        let _ = axum::serve(listener, router)
            .with_graceful_shutdown(async move {
                let _ = rx.await;
            })
            .await;
    });
    Gateway {
        addr,
        shutdown: Some(tx),
        handle: Some(handle),
    }
}

fn client() -> reqwest::Client {
    reqwest::Client::builder()
        // Keep the connection alive for SSE tests (default is fine; set
        // explicit timeouts to avoid hangs on failed assertions).
        .timeout(Duration::from_secs(10))
        .build()
        .unwrap()
}

/// Post a JSON body with admin auth and return the response.
async fn post_json(client: &reqwest::Client, url: &str, body: Value) -> reqwest::Response {
    client
        .post(url)
        .header("authorization", basic_auth_header())
        .header("content-type", "application/json")
        .body(body.to_string())
        .send()
        .await
        .unwrap()
}

/// Create a session on an already-enabled gateway and return its id.
async fn create_session_ok(gw: &Gateway, c: &reqwest::Client, ttl_secs: Option<u64>) -> String {
    let mut body = json!({
        "title": "t",
        "initial_state": {},
    });
    if let Some(t) = ttl_secs {
        body["ttl_secs"] = json!(t);
    }
    let resp = post_json(c, &gw.url("/canvas/session"), body).await;
    assert_eq!(
        resp.status(),
        reqwest::StatusCode::CREATED,
        "create session"
    );
    let v: Value = resp.json().await.unwrap();
    v["session_id"].as_str().unwrap().to_string()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[tokio::test]
async fn disabled_endpoints_return_503() {
    let gw = spawn_gateway(make_config(false)).await;
    let c = client();

    for (method, path, body) in [
        ("POST", "/canvas/session", Some(json!({}))),
        (
            "POST",
            "/canvas/frame",
            Some(json!({"session_id": "cs_00000000", "kind": "a2ui_push"})),
        ),
        ("GET", "/canvas/session/cs_00000000/events", None),
        // Iter 8 — `/canvas/render` shares the disabled gate.
        (
            "POST",
            "/canvas/render",
            Some(json!({
                "artifact_kind": "code",
                "body": {"language": "rust", "source": "fn main(){}"},
                "idempotency_key": "art_t",
            })),
        ),
    ] {
        let url = gw.url(path);
        let mut req = match method {
            "POST" => c.post(&url).body(body.unwrap().to_string()),
            "GET" => c.get(&url),
            _ => unreachable!(),
        };
        req = req
            .header("authorization", basic_auth_header())
            .header("content-type", "application/json");
        let resp = req.send().await.unwrap();
        assert_eq!(
            resp.status(),
            reqwest::StatusCode::SERVICE_UNAVAILABLE,
            "{method} {path}",
        );
        let v: Value = resp.json().await.unwrap();
        assert_eq!(v["error"], "canvas_host_disabled");
    }

    gw.shutdown().await;
}

#[tokio::test]
async fn create_session_returns_id_and_expiry() {
    let gw = spawn_gateway(make_config(true)).await;
    let c = client();

    let resp = post_json(
        &c,
        &gw.url("/canvas/session"),
        json!({
            "title": "Topology snapshot",
            "initial_state": {"foo": "bar"},
            "ttl_secs": 600,
        }),
    )
    .await;
    assert_eq!(resp.status(), reqwest::StatusCode::CREATED);
    let v: Value = resp.json().await.unwrap();
    let id = v["session_id"].as_str().unwrap();
    assert!(id.starts_with("cs_"), "session_id must be cs_-prefixed");
    assert_eq!(id.len(), 3 + 8, "session_id must be 3 + 8 chars");
    let created = v["created_at_ms"].as_u64().unwrap();
    let expires = v["expires_at_ms"].as_u64().unwrap();
    assert!(
        expires > created,
        "expires_at_ms must be after created_at_ms"
    );
    // 600_000 ms TTL (allow a generous jitter window for slow CI).
    assert!(
        (expires - created).abs_diff(600_000) < 5_000,
        "TTL window drifted: created={created} expires={expires}",
    );

    gw.shutdown().await;
}

#[tokio::test]
async fn post_frame_fans_out_to_sse_subscribers() {
    let gw = spawn_gateway(make_config(true)).await;
    let c = client();
    let id = create_session_ok(&gw, &c, Some(600)).await;

    // Open the SSE stream first.
    let stream_resp = c
        .get(gw.url(&format!("/canvas/session/{id}/events")))
        .header("authorization", basic_auth_header())
        .header("accept", "text/event-stream")
        .send()
        .await
        .unwrap();
    assert_eq!(stream_resp.status(), reqwest::StatusCode::OK);
    assert_eq!(
        stream_resp
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .unwrap_or(""),
        "text/event-stream"
    );

    // Spawn a reader that collects until we see our canvas frame.
    let reader = tokio::spawn(async move {
        let mut bytes = stream_resp.bytes_stream();
        let mut buf = String::new();
        while let Some(chunk) = bytes.next().await {
            let chunk = chunk.unwrap();
            buf.push_str(std::str::from_utf8(&chunk).unwrap());
            if buf.contains("event: canvas") && buf.contains("\n\n") {
                break;
            }
        }
        buf
    });

    // Give the subscriber a moment to actually register its broadcast rx
    // before we post the frame. 50ms is plenty on localhost and keeps the
    // test deterministic without a polling loop.
    tokio::time::sleep(Duration::from_millis(50)).await;

    let frame_resp = post_json(
        &c,
        &gw.url("/canvas/frame"),
        json!({
            "session_id": id,
            "kind": "a2ui_push",
            "payload": {"op": "set", "path": "/root", "value": 1},
        }),
    )
    .await;
    assert_eq!(frame_resp.status(), reqwest::StatusCode::ACCEPTED);
    let fv: Value = frame_resp.json().await.unwrap();
    assert!(fv["event_id"].is_string());

    let buf = tokio::time::timeout(Duration::from_secs(3), reader)
        .await
        .expect("sse reader timed out")
        .unwrap();
    assert!(buf.contains("event: canvas"), "sse payload: {buf}");
    assert!(buf.contains("\"kind\":\"a2ui_push\""), "sse payload: {buf}");
    assert!(buf.contains(&format!("\"session_id\":\"{id}\"")));

    gw.shutdown().await;
}

#[tokio::test]
async fn invalid_frame_kind_rejected_400() {
    let gw = spawn_gateway(make_config(true)).await;
    let c = client();
    let id = create_session_ok(&gw, &c, Some(600)).await;

    let resp = post_json(
        &c,
        &gw.url("/canvas/frame"),
        json!({
            "session_id": id,
            "kind": "delete_all_the_things",
            "payload": {},
        }),
    )
    .await;
    assert_eq!(resp.status(), reqwest::StatusCode::BAD_REQUEST);
    let v: Value = resp.json().await.unwrap();
    assert_eq!(v["error"], "invalid_frame_kind");
    assert!(v["allowed"].is_array());

    gw.shutdown().await;
}

#[tokio::test]
async fn unknown_session_returns_404() {
    let gw = spawn_gateway(make_config(true)).await;
    let c = client();

    // POST frame to a session that was never created.
    let resp = post_json(
        &c,
        &gw.url("/canvas/frame"),
        json!({
            "session_id": "cs_deadbeef",
            "kind": "a2ui_push",
            "payload": {},
        }),
    )
    .await;
    assert_eq!(resp.status(), reqwest::StatusCode::NOT_FOUND);

    // GET events for a session that was never created.
    let resp = c
        .get(gw.url("/canvas/session/cs_deadbeef/events"))
        .header("authorization", basic_auth_header())
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), reqwest::StatusCode::NOT_FOUND);

    gw.shutdown().await;
}

#[tokio::test]
async fn sse_stream_closes_on_session_expiry() {
    let gw = spawn_gateway(make_config(true)).await;
    let c = client();
    // ttl_secs is clamped to ≥1; use the smallest legal window so the
    // janitor tick after creation reaps it quickly.
    let id = create_session_ok(&gw, &c, Some(1)).await;

    let stream_resp = c
        .get(gw.url(&format!("/canvas/session/{id}/events")))
        .header("authorization", basic_auth_header())
        .header("accept", "text/event-stream")
        .send()
        .await
        .unwrap();
    assert_eq!(stream_resp.status(), reqwest::StatusCode::OK);

    // Read until we see `event: end`; the janitor runs once per second so
    // 4s gives it two chances to reap. The body stream ends when the task
    // returns — collect everything until EOF or we see the marker.
    let buf = tokio::time::timeout(Duration::from_secs(5), async {
        let mut bytes = stream_resp.bytes_stream();
        let mut buf = String::new();
        while let Some(chunk) = bytes.next().await {
            let chunk = chunk.unwrap();
            buf.push_str(std::str::from_utf8(&chunk).unwrap());
            if buf.contains("event: end") {
                break;
            }
        }
        buf
    })
    .await
    .expect("sse never signalled end");
    assert!(buf.contains("event: end"), "buf: {buf}");
    assert!(buf.contains("\"expired\""), "buf: {buf}");

    gw.shutdown().await;
}

#[tokio::test]
async fn auth_token_required_for_all_three_routes() {
    let gw = spawn_gateway(make_config(true)).await;
    let c = client();

    // POST /canvas/session without auth.
    let resp = c
        .post(gw.url("/canvas/session"))
        .header("content-type", "application/json")
        .body("{}")
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), reqwest::StatusCode::UNAUTHORIZED);

    // POST /canvas/frame without auth.
    let resp = c
        .post(gw.url("/canvas/frame"))
        .header("content-type", "application/json")
        .body(r#"{"session_id":"cs_00000000","kind":"a2ui_push"}"#)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), reqwest::StatusCode::UNAUTHORIZED);

    // GET /canvas/session/:id/events without auth.
    let resp = c
        .get(gw.url("/canvas/session/cs_00000000/events"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), reqwest::StatusCode::UNAUTHORIZED);

    gw.shutdown().await;
}

// ---------------------------------------------------------------------------
// Phase 4 W3 C3 iter 8 — `/canvas/render` integration tests.
//
// These hit the new synchronous renderer endpoint. Together with the
// disabled-route assertion at `disabled_endpoints_return_503` (which
// the iter-8 patch extends with `/canvas/render`) they cover:
//
//   - handshake (auth + disabled-config gating)
//   - happy path for each pure-Rust artifact kind (code/table/latex/sparkline)
//   - typed adapter-error surface for the gated mermaid build
//   - 400 on malformed payloads, 413 on oversize bodies
//
// The harness is the same `spawn_gateway` used by Phase-1 tests.
// ---------------------------------------------------------------------------

async fn render_ok(gw: &Gateway, c: &reqwest::Client, payload: Value) -> Value {
    let resp = post_json(c, &gw.url("/canvas/render"), payload).await;
    assert_eq!(resp.status(), reqwest::StatusCode::OK, "render");
    resp.json().await.unwrap()
}

#[tokio::test]
async fn render_requires_auth() {
    let gw = spawn_gateway(make_config(true)).await;
    let c = client();
    let resp = c
        .post(gw.url("/canvas/render"))
        .header("content-type", "application/json")
        .body(
            json!({
                "artifact_kind": "code",
                "body": {"language": "rust", "source": "fn main(){}"},
                "idempotency_key": "art_t",
            })
            .to_string(),
        )
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), reqwest::StatusCode::UNAUTHORIZED);
    gw.shutdown().await;
}

#[tokio::test]
async fn render_code_artifact_returns_html_and_hash() {
    let gw = spawn_gateway(make_config(true)).await;
    let c = client();

    let v = render_ok(
        &gw,
        &c,
        json!({
            "artifact_kind": "code",
            "body": {"language": "rust", "source": "fn main() { let x = 1; }"},
            "idempotency_key": "art_code",
            "theme_hint": "tp-light",
        }),
    )
    .await;

    let html = v["html_fragment"].as_str().unwrap();
    assert!(html.contains("cn-canvas-code"), "wrapper class: {html}");
    assert_eq!(v["render_kind"], "code");
    assert_eq!(v["theme_class"], "tp-light");
    let hash = v["content_hash"].as_str().unwrap();
    assert_eq!(hash.len(), 64, "blake3 hex: {hash}");
    assert!(hash.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));

    gw.shutdown().await;
}

#[tokio::test]
async fn render_table_markdown_artifact() {
    let gw = spawn_gateway(make_config(true)).await;
    let c = client();

    let v = render_ok(
        &gw,
        &c,
        json!({
            "artifact_kind": "table",
            "body": {"markdown": "| a | b |\n|---|---|\n| 1 | 2 |"},
            "idempotency_key": "art_table",
        }),
    )
    .await;

    let html = v["html_fragment"].as_str().unwrap();
    assert!(html.contains("<table"), "table tag: {html}");
    assert_eq!(v["render_kind"], "table");

    gw.shutdown().await;
}

#[tokio::test]
async fn render_latex_artifact() {
    let gw = spawn_gateway(make_config(true)).await;
    let c = client();

    let v = render_ok(
        &gw,
        &c,
        json!({
            "artifact_kind": "latex",
            "body": {"tex": "x^2 + 1", "display": false},
            "idempotency_key": "art_latex",
        }),
    )
    .await;

    let html = v["html_fragment"].as_str().unwrap();
    assert!(html.contains("katex"), "katex marker: {html}");
    assert_eq!(v["render_kind"], "latex");

    gw.shutdown().await;
}

#[tokio::test]
async fn render_sparkline_artifact() {
    let gw = spawn_gateway(make_config(true)).await;
    let c = client();

    let v = render_ok(
        &gw,
        &c,
        json!({
            "artifact_kind": "sparkline",
            "body": {"values": [1.0, 4.0, 2.0, 9.0], "unit": "tps"},
            "idempotency_key": "art_spark",
        }),
    )
    .await;

    let html = v["html_fragment"].as_str().unwrap();
    assert!(html.contains("<svg"), "svg tag: {html}");
    assert!(html.contains("cn-canvas-spark"), "wrapper class: {html}");
    assert_eq!(v["render_kind"], "sparkline");

    gw.shutdown().await;
}

#[tokio::test]
async fn render_mermaid_returns_adapter_error_when_feature_off() {
    // The default workspace build does NOT enable `corlinman-canvas`'s
    // `mermaid` feature (see crate Cargo.toml comment). The adapter
    // surfaces this as a typed `CanvasError::Adapter`, which the
    // gateway maps to 422 + `code: "adapter_error"`.
    let gw = spawn_gateway(make_config(true)).await;
    let c = client();

    let resp = post_json(
        &c,
        &gw.url("/canvas/render"),
        json!({
            "artifact_kind": "mermaid",
            "body": {"diagram": "graph LR; A-->B"},
            "idempotency_key": "art_merm",
        }),
    )
    .await;
    assert_eq!(resp.status(), reqwest::StatusCode::UNPROCESSABLE_ENTITY);
    let v: Value = resp.json().await.unwrap();
    assert_eq!(v["error"], "render_failed");
    assert_eq!(v["code"], "adapter_error");
    assert_eq!(v["artifact_kind"], "mermaid");

    gw.shutdown().await;
}

#[tokio::test]
async fn render_invalid_payload_400() {
    let gw = spawn_gateway(make_config(true)).await;
    let c = client();

    // Unknown artifact_kind — passes the byte cap, fails serde.
    let resp = post_json(
        &c,
        &gw.url("/canvas/render"),
        json!({
            "artifact_kind": "klingon",
            "body": {"x": 1},
            "idempotency_key": "art_bad",
        }),
    )
    .await;
    assert_eq!(resp.status(), reqwest::StatusCode::BAD_REQUEST);
    let v: Value = resp.json().await.unwrap();
    assert_eq!(v["error"], "invalid_payload");

    gw.shutdown().await;
}

#[tokio::test]
async fn render_body_too_large_413() {
    let gw = spawn_gateway(make_config(true)).await;
    let c = client();

    // 300 KB string — exceeds the 256 KB ceiling. Build the JSON
    // manually so the cost stays in `body_too_large` and not the
    // serde codepath.
    let huge = "a".repeat(300_000);
    let body = format!(
        r#"{{"artifact_kind":"code","body":{{"language":"rust","source":"{huge}"}},"idempotency_key":"art_big"}}"#
    );
    let resp = c
        .post(gw.url("/canvas/render"))
        .header("authorization", basic_auth_header())
        .header("content-type", "application/json")
        .body(body)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), reqwest::StatusCode::PAYLOAD_TOO_LARGE);
    let v: Value = resp.json().await.unwrap();
    assert_eq!(v["error"], "body_too_large");

    gw.shutdown().await;
}

#[tokio::test]
async fn render_is_cache_stable_across_calls() {
    // Same payload twice must return the same content_hash and the
    // same html_fragment. The shared renderer's LRU is exercised by
    // virtue of the second call hitting the cache; we only assert the
    // observable post-condition.
    let gw = spawn_gateway(make_config(true)).await;
    let c = client();

    let p = json!({
        "artifact_kind": "code",
        "body": {"language": "rust", "source": "fn main(){let x = 1;}"},
        "idempotency_key": "art_dedup",
        "theme_hint": "tp-dark",
    });
    let a = render_ok(&gw, &c, p.clone()).await;
    let b = render_ok(&gw, &c, p).await;

    assert_eq!(a["content_hash"], b["content_hash"]);
    assert_eq!(a["html_fragment"], b["html_fragment"]);
    assert_eq!(a["theme_class"], "tp-dark");

    gw.shutdown().await;
}

// ---------------------------------------------------------------------------
// Phase 4 W3 C3 iter 10 — E2E acceptance.
//
// Drives the full producer → gateway → SSE → renderer chain. The
// roadmap acceptance criterion (`phase4-roadmap.md:290-291`) is exact:
// *"Canvas Host renders a code block from agent output as
// syntax-highlighted HTML inside the admin UI."*  These tests assert
// the gateway side of that chain end-to-end; the UI vitest suite
// (`ui/components/canvas/canvas-artifact.test.tsx`) covers the DOM.
//
// They also lock down the iter-10 reconciliation between `present`
// frames and `/canvas/render`: producers POST `present` and get
// rendered HTML attached to the SSE event with no second round-trip;
// `/canvas/render` survives only as the stateless preview endpoint.
// ---------------------------------------------------------------------------

/// Acceptance: a producer-style `present` frame carrying a `code`
/// artifact yields an SSE `event: canvas` whose payload contains the
/// gateway-rendered, class-only HTML fragment.
///
/// This is the round-trip the C3 roadmap promises — closing the
/// renderer-on-frame path so that downstream UI consumers receive
/// rendered HTML inline with the SSE event, no second `/canvas/render`
/// round-trip needed.
#[tokio::test]
async fn e2e_code_block_round_trip_renders_in_present_frame() {
    let gw = spawn_gateway(make_config(true)).await;
    let c = client();
    let id = create_session_ok(&gw, &c, Some(600)).await;

    // Subscribe first so we don't miss the fan-out.
    let until_id = id.clone();
    let reader = tokio::spawn({
        let gw_url = gw.url(&format!("/canvas/session/{until_id}/events"));
        let auth = basic_auth_header();
        let cc = c.clone();
        async move {
            let stream_resp = cc
                .get(gw_url)
                .header("authorization", auth)
                .header("accept", "text/event-stream")
                .send()
                .await
                .unwrap();
            assert_eq!(stream_resp.status(), reqwest::StatusCode::OK);
            let mut bytes = stream_resp.bytes_stream();
            let mut buf = String::new();
            while let Some(chunk) = bytes.next().await {
                let chunk = chunk.unwrap();
                buf.push_str(std::str::from_utf8(&chunk).unwrap());
                if buf.contains("event: canvas") && buf.contains("\n\n") {
                    break;
                }
            }
            buf
        }
    });

    // Settle the SSE subscription before producing.
    tokio::time::sleep(Duration::from_millis(80)).await;

    // Producer-style frame: `present` with a closed C3 payload.
    let frame_resp = post_json(
        &c,
        &gw.url("/canvas/frame"),
        json!({
            "session_id": id,
            "kind": "present",
            "payload": {
                "artifact_kind": "code",
                "body": {"language": "rust", "source": "fn main() { println!(\"ok\"); }"},
                "idempotency_key": "art_e2e_code",
                "theme_hint": "tp-light",
            },
        }),
    )
    .await;
    assert_eq!(frame_resp.status(), reqwest::StatusCode::ACCEPTED);
    let frame_body: Value = frame_resp.json().await.unwrap();
    assert_eq!(frame_body["idempotency_key"], "art_e2e_code");

    // Read the SSE event.
    let buf = tokio::time::timeout(Duration::from_secs(5), reader)
        .await
        .expect("sse reader timed out")
        .unwrap();
    assert!(buf.contains("event: canvas"), "sse buf: {buf}");

    // Find the JSON payload line and parse it.
    let data_line = buf
        .lines()
        .find(|l| l.starts_with("data: "))
        .expect("no data line in sse buf");
    let parsed: Value =
        serde_json::from_str(&data_line["data: ".len()..]).expect("data line not JSON");
    assert_eq!(parsed["kind"], "present");

    // Acceptance: `rendered` is attached, contains class-only HTML
    // with at least one syntect token span and the wrapper class.
    let rendered = &parsed["payload"]["rendered"];
    assert!(rendered.is_object(), "rendered missing: {parsed}");
    assert_eq!(rendered["render_kind"], "code");
    assert_eq!(rendered["theme_class"], "tp-light");
    let html = rendered["html_fragment"]
        .as_str()
        .expect("html_fragment string");
    assert!(html.contains("cn-canvas-code"), "wrapper class: {html}");
    // Syntect emits `<span class="…">` for token classes; with rust
    // input we should see at least one classed span.
    assert!(html.contains("<span"), "expected token spans: {html}");
    // The original text must survive escaping. Syntect wraps each
    // token in its own span, so `fn` and `main` end up in distinct
    // spans — assert each separately. The string literal "ok" also
    // survives quoting (HTML-escaped to `&quot;`).
    assert!(html.contains(">fn</span>"), "fn keyword span: {html}");
    assert!(html.contains(">main</span>"), "main name span: {html}");
    assert!(html.contains("&quot;"), "string quoting escaped: {html}");
    // 64-char blake3 hex content_hash.
    let hash = rendered["content_hash"].as_str().unwrap();
    assert_eq!(hash.len(), 64);
    assert!(hash.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));

    gw.shutdown().await;
}

/// Iter 10 — `present`-frame idempotency. Two POSTs with the same
/// `(session_id, idempotency_key)` produce one fan-out event; the
/// second hit returns 200 + `deduped: true` and no new SSE frame.
#[tokio::test]
async fn e2e_present_frame_idempotency_key_dedupes() {
    let gw = spawn_gateway(make_config(true)).await;
    let c = client();
    let id = create_session_ok(&gw, &c, Some(600)).await;

    let payload = json!({
        "session_id": id,
        "kind": "present",
        "payload": {
            "artifact_kind": "code",
            "body": {"language": "rust", "source": "fn main(){}"},
            "idempotency_key": "art_dedupe_e2e",
        },
    });

    // First POST — accepted.
    let first = post_json(&c, &gw.url("/canvas/frame"), payload.clone()).await;
    assert_eq!(first.status(), reqwest::StatusCode::ACCEPTED);
    let first_body: Value = first.json().await.unwrap();
    assert!(first_body["event_id"].is_string());

    // Second POST — same key, should dedupe.
    let second = post_json(&c, &gw.url("/canvas/frame"), payload).await;
    assert_eq!(second.status(), reqwest::StatusCode::OK);
    let second_body: Value = second.json().await.unwrap();
    assert_eq!(second_body["deduped"], true);
    assert_eq!(second_body["idempotency_key"], "art_dedupe_e2e");
    // event_id must be null on dedupe — no new fan-out fired.
    assert!(second_body["event_id"].is_null());

    gw.shutdown().await;
}

/// Iter 10 — non-C3 `present` frames (legacy a2ui-style payloads
/// without the closed `artifact_kind`/`body` shape) pass through
/// verbatim. The gateway speculatively tries C3 deserialisation; on
/// miss the frame is fanned out unchanged. This protects pre-C3
/// callers from breaking when iter 10 lands enrichment.
#[tokio::test]
async fn e2e_present_frame_passes_through_legacy_payload() {
    let gw = spawn_gateway(make_config(true)).await;
    let c = client();
    let id = create_session_ok(&gw, &c, Some(600)).await;

    let until_id = id.clone();
    let reader = tokio::spawn({
        let gw_url = gw.url(&format!("/canvas/session/{until_id}/events"));
        let auth = basic_auth_header();
        let cc = c.clone();
        async move {
            let stream_resp = cc
                .get(gw_url)
                .header("authorization", auth)
                .header("accept", "text/event-stream")
                .send()
                .await
                .unwrap();
            let mut bytes = stream_resp.bytes_stream();
            let mut buf = String::new();
            while let Some(chunk) = bytes.next().await {
                let chunk = chunk.unwrap();
                buf.push_str(std::str::from_utf8(&chunk).unwrap());
                if buf.contains("event: canvas") && buf.contains("\n\n") {
                    break;
                }
            }
            buf
        }
    });
    tokio::time::sleep(Duration::from_millis(80)).await;

    let frame_resp = post_json(
        &c,
        &gw.url("/canvas/frame"),
        json!({
            "session_id": id,
            "kind": "present",
            // Legacy a2ui-style payload — no `artifact_kind` field.
            "payload": {"component": "Box", "props": {"children": "hello"}},
        }),
    )
    .await;
    assert_eq!(frame_resp.status(), reqwest::StatusCode::ACCEPTED);

    let buf = tokio::time::timeout(Duration::from_secs(5), reader)
        .await
        .expect("sse reader timed out")
        .unwrap();
    let data_line = buf.lines().find(|l| l.starts_with("data: ")).unwrap();
    let parsed: Value =
        serde_json::from_str(&data_line["data: ".len()..]).expect("data line not JSON");
    // No `rendered` key — legacy payload survives untouched.
    assert!(parsed["payload"]["rendered"].is_null());
    // The original `component` / `props` keys are still there.
    assert_eq!(parsed["payload"]["component"], "Box");

    gw.shutdown().await;
}

/// Iter 10 — when the renderer rejects a payload (e.g. mermaid with
/// the feature gated off), the gateway still fans the frame out and
/// attaches a structured `render_error` so the UI's error panel can
/// render. The producer is NOT 4xx'd: the producer's job is to push
/// the artifact, not to know about the consumer's renderer build.
#[tokio::test]
async fn e2e_present_frame_attaches_render_error_on_failure() {
    let gw = spawn_gateway(make_config(true)).await;
    let c = client();
    let id = create_session_ok(&gw, &c, Some(600)).await;

    let frame_resp = post_json(
        &c,
        &gw.url("/canvas/frame"),
        json!({
            "session_id": id,
            "kind": "present",
            "payload": {
                "artifact_kind": "mermaid",
                "body": {"diagram": "graph LR; A-->B"},
                "idempotency_key": "art_merm_e2e",
            },
        }),
    )
    .await;
    assert_eq!(frame_resp.status(), reqwest::StatusCode::ACCEPTED);

    // Read directly from the events list via a fresh subscriber: the
    // event has already fanned, but a new subscriber sees only future
    // frames, so query the SSE in racy mode after the post (the
    // `subscribers.send` happens inside the write lock so we open the
    // subscriber on a tiny fresh post to flush).
    let until_id = id.clone();
    let reader = tokio::spawn({
        let gw_url = gw.url(&format!("/canvas/session/{until_id}/events"));
        let auth = basic_auth_header();
        let cc = c.clone();
        async move {
            let stream_resp = cc
                .get(gw_url)
                .header("authorization", auth)
                .header("accept", "text/event-stream")
                .send()
                .await
                .unwrap();
            let mut bytes = stream_resp.bytes_stream();
            let mut buf = String::new();
            while let Some(chunk) = bytes.next().await {
                let chunk = chunk.unwrap();
                buf.push_str(std::str::from_utf8(&chunk).unwrap());
                if buf.contains("render_error") && buf.contains("\n\n") {
                    break;
                }
            }
            buf
        }
    });
    tokio::time::sleep(Duration::from_millis(80)).await;

    // Re-issue (different idempotency key) so the new subscriber
    // sees the fan-out.
    let _ = post_json(
        &c,
        &gw.url("/canvas/frame"),
        json!({
            "session_id": id,
            "kind": "present",
            "payload": {
                "artifact_kind": "mermaid",
                "body": {"diagram": "graph LR; A-->B"},
                "idempotency_key": "art_merm_e2e_2",
            },
        }),
    )
    .await;

    let buf = tokio::time::timeout(Duration::from_secs(5), reader)
        .await
        .expect("sse reader timed out")
        .unwrap();
    let data_line = buf
        .lines()
        .find(|l| l.starts_with("data: ") && l.contains("render_error"))
        .expect("no render_error frame");
    let parsed: Value =
        serde_json::from_str(&data_line["data: ".len()..]).expect("data line not JSON");
    assert_eq!(parsed["kind"], "present");
    let err = &parsed["payload"]["render_error"];
    assert_eq!(err["code"], "adapter_error");
    assert_eq!(err["artifact_kind"], "mermaid");

    gw.shutdown().await;
}

/// Iter 10 — `[canvas] max_artifact_bytes` is honoured live from
/// config. With a tiny cap on the gateway, even a small `present`
/// payload triggers `413 body_too_large`. Closes the iter-9 gap that
/// these knobs were `const`-stopgaps in `routes/canvas.rs`.
#[tokio::test]
async fn e2e_present_frame_respects_config_max_artifact_bytes() {
    let mut cfg = make_config(true);
    cfg.canvas.max_artifact_bytes = 1024; // tiny cap
    let gw = spawn_gateway(cfg).await;
    let c = client();
    let id = create_session_ok(&gw, &c, Some(600)).await;

    // 2 KiB source — well over the 1 KiB cap.
    let big_source = "a".repeat(2048);
    let resp = post_json(
        &c,
        &gw.url("/canvas/frame"),
        json!({
            "session_id": id,
            "kind": "present",
            "payload": {
                "artifact_kind": "code",
                "body": {"language": "rust", "source": big_source},
                "idempotency_key": "art_bigframe",
            },
        }),
    )
    .await;
    assert_eq!(resp.status(), reqwest::StatusCode::PAYLOAD_TOO_LARGE);
    let v: Value = resp.json().await.unwrap();
    assert_eq!(v["error"], "body_too_large");
    assert_eq!(v["max_bytes"], 1024);

    gw.shutdown().await;
}

/// Iter 10 — `[canvas] cache_max_entries` is honoured at gateway
/// boot. With cache disabled (`0`), the renderer's LRU never grows.
/// Closes the iter-9 config-knob gap on the cache-side.
#[tokio::test]
async fn e2e_canvas_cache_disabled_when_config_zero() {
    let mut cfg = make_config(true);
    cfg.canvas.cache_max_entries = 0;
    let gw = spawn_gateway(cfg).await;
    let c = client();

    // Two distinct renders — both must succeed even with cache off.
    let _ = render_ok(
        &gw,
        &c,
        json!({
            "artifact_kind": "code",
            "body": {"language": "rust", "source": "fn a(){}"},
            "idempotency_key": "art_a",
        }),
    )
    .await;
    let _ = render_ok(
        &gw,
        &c,
        json!({
            "artifact_kind": "code",
            "body": {"language": "rust", "source": "fn b(){}"},
            "idempotency_key": "art_b",
        }),
    )
    .await;

    // No direct introspection exposed to integration tests; the
    // observable post-condition is "doesn't crash, returns identical
    // shape as the cached path". The unit test
    // `corlinman_canvas::tests::disabled_cache_does_not_grow`
    // already asserts the zero-grow invariant.
    gw.shutdown().await;
}
