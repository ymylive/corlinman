//! B5-BE6 — consolidated final regression.
//!
//! Five scenarios that pin Batch 5-specific behaviours at the cross-crate
//! harness layer, exercising the public APIs of every crate that landed in
//! Batch 5 (`canvas` route, `NodeBridgeServer`, `ConfigWatcher`, metrics
//! facade) + a compact "still works together" chain.
//!
//! Rules of the road for this file:
//!   - No mock-outs of internal state. Every scenario composes public APIs.
//!   - Loopback + ephemeral ports only (`127.0.0.1:0`).
//!   - `tokio::time::pause()` / `advance()` where timing matters.
//!   - Per-phase deadlines (reqwest timeout, hook-bus spin) are tight;
//!     overall wall-clock budgets are catastrophic-regression sentinels
//!     and must absorb workspace-level CPU contention. See
//!     `full_batch_chain`'s closing assertion for the rationale.
//!
//! These are the "did someone quietly regress a B5 behaviour?" regression
//! tripwires; detailed behaviour is covered by each crate's own test
//! module.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use arc_swap::ArcSwap;
use axum::Router;
use base64::Engine;
use corlinman_core::config::Config;
use corlinman_gateway::config_watcher::ConfigWatcher;
use corlinman_gateway::metrics;
use corlinman_gateway::middleware::admin_auth::AdminAuthState;
use corlinman_gateway::routes::canvas::{self, CanvasState};
use corlinman_gateway::routes::metrics as metrics_route;
use corlinman_hooks::{HookBus, HookEvent, HookPriority};
use corlinman_nodebridge::{
    Capability, NodeBridgeMessage, NodeBridgeServer, NodeBridgeServerConfig,
};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::net::TcpListener;
use tokio::sync::oneshot;
use tokio::time::Instant;
use tokio_tungstenite::tungstenite::Message as TungMessage;
use tokio_util::sync::CancellationToken;
use tower::ServiceExt;

// ---------------------------------------------------------------------------
// Small admin + gateway harness shared by the canvas + full-chain tests.
// Mirrors the pattern used by the gateway's own `canvas_host.rs` so there
// is exactly one way to spin up the canvas router in this repo.
// ---------------------------------------------------------------------------

const ADMIN_USER: &str = "admin";
const ADMIN_PASS: &str = "secret";

fn hash_password(password: &str) -> String {
    use argon2::password_hash::{PasswordHasher, SaltString};
    // Stable salt so the hash is deterministic across parallel tests.
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

fn canvas_config(enabled: bool) -> Config {
    let mut cfg = Config::default();
    cfg.admin.username = Some(ADMIN_USER.into());
    cfg.admin.password_hash = Some(hash_password(ADMIN_PASS));
    cfg.canvas.host_endpoint_enabled = enabled;
    cfg
}

struct SpawnedGateway {
    addr: SocketAddr,
    shutdown: Option<oneshot::Sender<()>>,
    handle: Option<tokio::task::JoinHandle<()>>,
}

impl SpawnedGateway {
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

async fn spawn_canvas_gateway(config: Config) -> SpawnedGateway {
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
    SpawnedGateway {
        addr,
        shutdown: Some(tx),
        handle: Some(handle),
    }
}

// ---------------------------------------------------------------------------
// Test 1 — Canvas session round-trip (B5-BE1).
//
// Boot gateway with `[canvas] host_endpoint_enabled = true`, create a
// session via the real HTTP route, post a frame, and verify it arrives on
// the SSE stream. This is the "the transport still works end to end"
// tripwire.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn canvas_session_roundtrip() {
    let gw = spawn_canvas_gateway(canvas_config(true)).await;
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .unwrap();

    // Create session.
    let resp = client
        .post(gw.url("/canvas/session"))
        .header("authorization", basic_auth_header())
        .header("content-type", "application/json")
        .body(json!({"title": "b5-final", "ttl_secs": 600}).to_string())
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), reqwest::StatusCode::CREATED);
    let v: Value = resp.json().await.unwrap();
    let session_id = v["session_id"].as_str().unwrap().to_string();
    assert!(session_id.starts_with("cs_"));

    // Open SSE first so the subscriber is live before the frame is posted.
    let stream_resp = client
        .get(gw.url(&format!("/canvas/session/{session_id}/events")))
        .header("authorization", basic_auth_header())
        .header("accept", "text/event-stream")
        .send()
        .await
        .unwrap();
    assert_eq!(stream_resp.status(), reqwest::StatusCode::OK);

    // Drain until we see the canvas frame, with a budget well under 5s.
    let reader_session_id = session_id.clone();
    let reader = tokio::spawn(async move {
        let mut bytes = stream_resp.bytes_stream();
        let mut buf = String::new();
        while let Some(chunk) = bytes.next().await {
            let chunk = chunk.unwrap();
            buf.push_str(std::str::from_utf8(&chunk).unwrap());
            if buf.contains("event: canvas")
                && buf.contains(&format!("\"session_id\":\"{reader_session_id}\""))
            {
                break;
            }
        }
        buf
    });

    // Small wait for the broadcast::Receiver to register before we post.
    tokio::time::sleep(Duration::from_millis(50)).await;

    let frame_resp = client
        .post(gw.url("/canvas/frame"))
        .header("authorization", basic_auth_header())
        .header("content-type", "application/json")
        .body(
            json!({
                "session_id": session_id,
                "kind": "a2ui_push",
                "payload": {"op": "set", "path": "/root", "value": 42},
            })
            .to_string(),
        )
        .send()
        .await
        .unwrap();
    assert_eq!(frame_resp.status(), reqwest::StatusCode::ACCEPTED);

    let buf = tokio::time::timeout(Duration::from_secs(3), reader)
        .await
        .expect("sse reader timed out")
        .unwrap();
    assert!(buf.contains("event: canvas"), "sse payload: {buf}");
    assert!(buf.contains("\"kind\":\"a2ui_push\""), "sse payload: {buf}");
    assert!(buf.contains("\"value\":42"), "sse payload: {buf}");

    gw.shutdown().await;
}

// ---------------------------------------------------------------------------
// Test 2 — NodeBridge register → dispatch → telemetry (B5-BE2).
//
// A fake client registers with capability "hello", the server dispatches
// a job of kind "hello", the client responds, the client then emits a
// Telemetry frame, and a hook-bus subscriber observes the corresponding
// `HookEvent::Telemetry`.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn nodebridge_register_dispatch_telemetry() {
    let hook_bus = Arc::new(HookBus::new(64));
    let mut sub = hook_bus.subscribe(HookPriority::Normal);

    let cfg = NodeBridgeServerConfig::loopback(true);
    let server = Arc::new(NodeBridgeServer::new(cfg, hook_bus.clone()));
    let addr = server.bind().await.expect("bind nodebridge");
    let ws_url = format!("ws://{addr}/nodebridge/connect");

    // Dial + register with capability "hello".
    let (mut ws, _) = tokio_tungstenite::connect_async(&ws_url)
        .await
        .expect("ws connect");
    let register = NodeBridgeMessage::Register {
        node_id: "b5-node".into(),
        node_type: "other".into(),
        capabilities: vec![Capability::new("hello", "1.0", json!({"type": "object"}))],
        auth_token: "tok".into(),
        version: "0.1.0".into(),
        signature: None,
    };
    ws.send(TungMessage::Text(serde_json::to_string(&register).unwrap()))
        .await
        .unwrap();

    // Drain the `Registered` ack.
    let ack = ws.next().await.expect("ack").expect("ws frame");
    match ack {
        TungMessage::Text(t) => {
            let decoded: NodeBridgeMessage = serde_json::from_str(&t).unwrap();
            assert!(
                matches!(decoded, NodeBridgeMessage::Registered { .. }),
                "expected Registered, got {decoded:?}"
            );
        }
        other => panic!("unexpected first frame: {other:?}"),
    }

    // Wait until the server has indexed the capability, else dispatch_job
    // races and returns NoCapableNode.
    let deadline = Instant::now() + Duration::from_secs(2);
    while server.connected_count() == 0 {
        if Instant::now() > deadline {
            panic!("capability never indexed");
        }
        tokio::task::yield_now().await;
    }

    // Fake client: respond to the dispatched job with ok=true, then emit
    // a Telemetry frame back through the socket.
    let responder = tokio::spawn(async move {
        while let Some(Ok(frame)) = ws.next().await {
            let TungMessage::Text(text) = frame else {
                continue;
            };
            let parsed: NodeBridgeMessage = match serde_json::from_str(&text) {
                Ok(p) => p,
                Err(_) => continue,
            };
            if let NodeBridgeMessage::DispatchJob { job_id, kind, .. } = parsed {
                assert_eq!(kind, "hello");
                let result = NodeBridgeMessage::JobResult {
                    job_id,
                    ok: true,
                    payload: json!({"greeting": "hi"}),
                };
                ws.send(TungMessage::Text(serde_json::to_string(&result).unwrap()))
                    .await
                    .unwrap();

                // Follow-up telemetry emission on the same socket.
                let mut tags = std::collections::BTreeMap::new();
                tags.insert("build".into(), "dev".into());
                let tele = NodeBridgeMessage::Telemetry {
                    node_id: "b5-node".into(),
                    metric: "hello.ok".into(),
                    value: 1.0,
                    tags,
                };
                ws.send(TungMessage::Text(serde_json::to_string(&tele).unwrap()))
                    .await
                    .unwrap();
                return;
            }
        }
    });

    // Server dispatches a `hello` job and awaits the reply.
    let result = server
        .dispatch_job("hello", json!({"who": "world"}), 2_000)
        .await
        .expect("dispatch ok");
    match result {
        NodeBridgeMessage::JobResult { ok, payload, .. } => {
            assert!(ok);
            assert_eq!(payload, json!({"greeting": "hi"}));
        }
        other => panic!("expected JobResult, got {other:?}"),
    }

    // Telemetry → HookEvent::Telemetry, observed by subscriber.
    let observed = tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            match sub.recv().await {
                Ok(HookEvent::Telemetry {
                    node_id,
                    metric,
                    value,
                    ..
                }) => return (node_id, metric, value),
                Ok(_) => continue,
                Err(err) => panic!("subscribe recv error: {err:?}"),
            }
        }
    })
    .await
    .expect("telemetry hook event within 2s");

    assert_eq!(observed.0, "b5-node");
    assert_eq!(observed.1, "hello.ok");
    assert!((observed.2 - 1.0).abs() < 1e-9);

    responder.abort();
    let _ = responder.await;
    server.shutdown().await;
}

// ---------------------------------------------------------------------------
// Test 3 — Config hot-reload flips a feature flag (B5-BE3).
//
// Write a config file with `[tools.block] enabled = false`, spawn the
// watcher, rewrite with `enabled = true`, assert:
//   * `ConfigChanged { section: "tools", .. }` fires on the hook bus,
//   * the live snapshot reflects the new value.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn config_hot_reload_flips_feature_flag() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("config.toml");

    let initial = Config::default();
    assert!(
        !initial.tools.block.enabled,
        "fixture expects default tools.block.enabled=false"
    );
    std::fs::write(&path, toml::to_string_pretty(&initial).unwrap()).unwrap();

    let bus = Arc::new(HookBus::new(64));
    let watcher = Arc::new(ConfigWatcher::new(path.clone(), initial, bus.clone()));
    let cancel = CancellationToken::new();
    let run_task = {
        let w = watcher.clone();
        let c = cancel.clone();
        tokio::spawn(async move { w.run(c).await })
    };

    // Give the fs watcher a moment to install (FSEvents on macOS is slow).
    tokio::time::sleep(Duration::from_millis(200)).await;

    let mut sub = bus.subscribe(HookPriority::Normal);

    // Rewrite with the flag flipped. Atomic rename matches the admin
    // endpoint + other reload tests.
    let mut next = Config::default();
    next.tools.block.enabled = true;
    let mut tmp_path = path.clone();
    tmp_path.as_mut_os_string().push(".tmp");
    std::fs::write(&tmp_path, toml::to_string_pretty(&next).unwrap()).unwrap();
    std::fs::rename(&tmp_path, &path).unwrap();

    // Wait for the ConfigChanged{section="tools"} event. 4s is generous
    // for fs-notify + debounce on CI; local runs fire inside ~500ms.
    let observed = tokio::time::timeout(Duration::from_secs(4), async {
        loop {
            match sub.recv().await {
                Ok(HookEvent::ConfigChanged { section, new, .. }) if section == "tools" => {
                    return new;
                }
                Ok(_) => continue,
                Err(err) => panic!("subscribe recv error: {err:?}"),
            }
        }
    })
    .await
    .expect("expected tools ConfigChanged within 4s");

    assert_eq!(
        observed
            .get("block")
            .and_then(|b| b.get("enabled"))
            .and_then(Value::as_bool),
        Some(true),
        "tools.block.enabled must be true in emitted new-state, got {observed:?}",
    );

    // And the current snapshot reflects the swap.
    assert!(
        watcher.current().tools.block.enabled,
        "live config snapshot must reflect the reload"
    );

    cancel.cancel();
    let _ = run_task.await;
}

// ---------------------------------------------------------------------------
// Test 4 — `/metrics` exposes every B1–B4 counter family added by B5-BE4.
//
// This is the "no one silently removed a counter" sentinel: we hit the
// real `/metrics` route and assert every registered family name appears.
// Paired with the gateway-local test of the same shape in
// `gateway/tests/metrics_endpoint_exposes_new_counters.rs`, but lives
// here so a removal that passes per-crate tests still fails the
// cross-crate regression.
// ---------------------------------------------------------------------------

const EXPECTED_METRIC_FAMILIES: &[&str] = &[
    "corlinman_protocol_dispatch_total",
    "corlinman_protocol_dispatch_errors_total",
    "corlinman_wstool_invokes_total",
    "corlinman_wstool_invoke_duration_seconds",
    "corlinman_wstool_runners_connected",
    "corlinman_file_fetcher_fetches_total",
    "corlinman_file_fetcher_bytes_total",
    "corlinman_telegram_updates_total",
    "corlinman_telegram_media_total",
    "corlinman_hook_emits_total",
    "corlinman_hook_subscribers_current",
    "corlinman_skill_invocations_total",
    "corlinman_agent_mutes_total",
    "corlinman_rate_limit_triggers_total",
    "corlinman_approvals_total",
];

#[tokio::test]
async fn metrics_endpoint_contains_all_b1_through_b5_counters() {
    metrics::init();

    let app = metrics_route::router();
    let req = axum::http::Request::builder()
        .method("GET")
        .uri("/metrics")
        .body(axum::body::Body::empty())
        .unwrap();
    let resp = app.oneshot(req).await.expect("metrics handler ran");
    assert_eq!(resp.status(), axum::http::StatusCode::OK);

    let body = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .expect("body read");
    let text = String::from_utf8(body.to_vec()).expect("utf8 scrape body");

    for needle in EXPECTED_METRIC_FAMILIES {
        assert!(
            text.contains(needle),
            "missing metric family `{needle}` in /metrics scrape — did B5-BE4's \
             pre-registration regress?",
        );
    }

    // Sanity-check the sentinel is exactly as long as we documented.
    assert_eq!(
        EXPECTED_METRIC_FAMILIES.len(),
        15,
        "regression guard expects 15 B5-tracked counter families",
    );
}

// ---------------------------------------------------------------------------
// Test 5 — Condensed full-batch chain.
//
// Touches every Batch in one test:
//   * B1: HookBus + HookEvent emission (ConfigChanged path).
//   * B3: block-dispatcher metric increments (exercised implicitly by the
//         metrics counter we assert here; covered end-to-end in
//         `b4_full_chain.rs` so we only sanity-check the facade here).
//   * B4: emit a MessageSent + ToolCalled and see both on a subscriber.
//   * B5: canvas session (HTTP) + nodebridge register (WS) wired against
//         the same `HookBus` via separate servers.
//
// Budget: well under 5s. `tokio::time::pause()` is intentionally NOT used
// here because all we need is bounded spin waits on side-effects.
// ---------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn full_batch_chain() {
    let overall = Instant::now();

    // ---------- Hook bus (B1) + subscriber on Normal tier ----------
    let bus = Arc::new(HookBus::new(64));
    let mut sub = bus.subscribe(HookPriority::Normal);

    // ---------- Canvas HTTP stack (B5-BE1) ----------
    let gw = spawn_canvas_gateway(canvas_config(true)).await;
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(3))
        .build()
        .unwrap();
    let resp = client
        .post(gw.url("/canvas/session"))
        .header("authorization", basic_auth_header())
        .header("content-type", "application/json")
        .body(json!({"title": "chain", "ttl_secs": 60}).to_string())
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), reqwest::StatusCode::CREATED);
    let session_id = resp.json::<Value>().await.unwrap()["session_id"]
        .as_str()
        .unwrap()
        .to_string();
    assert!(session_id.starts_with("cs_"));

    // ---------- NodeBridge server + single fake client (B5-BE2) ----------
    let nb_server = Arc::new(NodeBridgeServer::new(
        NodeBridgeServerConfig::loopback(true),
        bus.clone(),
    ));
    let nb_addr = nb_server.bind().await.expect("nb bind");
    let nb_ws_url = format!("ws://{nb_addr}/nodebridge/connect");
    let (mut ws, _) = tokio_tungstenite::connect_async(&nb_ws_url)
        .await
        .expect("ws connect");
    let register = NodeBridgeMessage::Register {
        node_id: "chain-node".into(),
        node_type: "other".into(),
        capabilities: vec![Capability::new("chain", "1.0", json!({}))],
        auth_token: "tok".into(),
        version: "0.1.0".into(),
        signature: None,
    };
    ws.send(TungMessage::Text(serde_json::to_string(&register).unwrap()))
        .await
        .unwrap();
    // Drain the Registered ack.
    let _ = ws.next().await.expect("ack");
    // Wait until the session is visible.
    let deadline = Instant::now() + Duration::from_secs(2);
    while nb_server.connected_count() == 0 {
        if Instant::now() > deadline {
            panic!("nb session never registered");
        }
        tokio::task::yield_now().await;
    }

    // ---------- Metric facade touch (B5-BE4) ----------
    // Even without dispatching a real block here we want the gateway's
    // `metrics::init` to have pre-registered the counters it promises.
    metrics::init();

    // ---------- B4 fan-out on hooks: emit ToolCalled + MessageSent ----------
    bus.emit(HookEvent::ToolCalled {
        tool: "adder".into(),
        runner_id: "local".into(),
        duration_ms: 1,
        ok: true,
        error_code: None,
        tenant_id: None,
        user_id: None,
    })
    .await
    .expect("emit tool_called");
    bus.emit(HookEvent::MessageSent {
        channel: "telegram".into(),
        session_key: "telegram:1:1".into(),
        content: "hi".into(),
        success: true,
        user_id: None,
    })
    .await
    .expect("emit message_sent");

    // Assert both events were observed. Don't rely on strict ordering
    // against other events that subsystem shutdown might emit — just that
    // both shapes surfaced in a 1s budget.
    let mut saw_tool = false;
    let mut saw_sent = false;
    let deadline = Instant::now() + Duration::from_secs(1);
    while (!saw_tool || !saw_sent) && Instant::now() < deadline {
        match tokio::time::timeout(Duration::from_millis(200), sub.recv()).await {
            Ok(Ok(HookEvent::ToolCalled { .. })) => saw_tool = true,
            Ok(Ok(HookEvent::MessageSent { .. })) => saw_sent = true,
            _ => {}
        }
    }
    assert!(saw_tool, "ToolCalled event missing on Normal tier");
    assert!(saw_sent, "MessageSent event missing on Normal tier");

    // ---------- Cleanup ----------
    gw.shutdown().await;
    nb_server.shutdown().await;

    // Wall-clock budget is a catastrophic-regression sentinel, not a
    // micro-benchmark. The internal sub-deadlines (3s reqwest, 2s
    // nodebridge spin, 1s hook-bus spin) already pin each phase tightly;
    // this assertion only catches a multi-second regression slipping
    // past every per-phase guard.
    //
    // Workspace-pressure flake fix: under `cargo test --workspace`
    // there are 25+ test binaries racing for CPU and the scheduler. Two
    // argon2 hashes (handler-side verify on POST + the test-side hash
    // in `canvas_config`), a NodeBridge WS handshake, two server
    // boots and the reqwest client init can credibly run past 5s of
    // wall clock even though no individual sub-deadline trips. Per-run
    // local timing on a hot cache stays ~150-300ms, but contended runs
    // observed up to ~6-8s — well below the per-deadline ceilings, but
    // over the previous 5s sentinel.
    //
    // Phase 4 Wave 3+4 close-out merge bumped the workspace test load
    // substantially (8 stream branches' worth of new integration tests
    // running in parallel under `cargo test --workspace`), pushing
    // observed wall-clock to 30-65s on this contended path even though
    // no individual sub-deadline tripped. Raise the sentinel to 120s
    // so it stays a smoke-detector for the synchronous-boot-loop class
    // of bug (those would run for minutes) without red-flagging healthy
    // contended runs. The per-deadline ceilings inside the chain
    // continue to enforce the actual correctness invariants.
    let elapsed = overall.elapsed();
    assert!(
        elapsed < Duration::from_secs(120),
        "full_batch_chain must complete under 120s, took {elapsed:?}",
    );
}
