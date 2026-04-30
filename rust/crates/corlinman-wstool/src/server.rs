//! `WsToolServer` — the in-gateway half of the distributed tool bus.
//!
//! A runner dials `GET /wstool/connect?auth_token=…&runner_id=…&version=…`;
//! we upgrade to a WebSocket, validate the token, then enter a long-lived
//! per-connection task that multiplexes many concurrent `Invoke`
//! request/reply pairs over one socket.
//!
//! Shared state:
//!   - [`ServerState::runners`] — `runner_id -> ConnHandle` for write-path
//!     and outstanding-request bookkeeping.
//!   - [`ServerState::tool_index`] — `tool_name -> runner_id`. First
//!     runner to advertise a tool wins; disconnected runners are purged.
//!
//! Heartbeats: the connection task owns a `tokio::time::interval` ticking
//! every `heartbeat_secs` seconds. Each tick sends `Ping`; unanswered
//! pings are counted and the connection is dropped after three misses.
//! `tokio::time::pause()` in tests advances this deterministically.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use axum::extract::ws::{Message as WsMessage, WebSocket, WebSocketUpgrade};
use axum::extract::{ConnectInfo, Query, State};
use axum::response::IntoResponse;
use axum::routing::get;
use axum::Router;
use dashmap::DashMap;
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use tokio::sync::{mpsc, oneshot, Mutex};
use tokio::task::JoinHandle;
use tokio::time::{Instant, MissedTickBehavior};
use tracing::{debug, info, warn};

use corlinman_hooks::{HookBus, HookEvent};

use crate::error::WsToolError;
use crate::message::{ToolAdvert, WsToolMessage};
use crate::runtime::WsToolRuntime;

/// Wire-level configuration handed to the server.
///
/// Kept independent of [`corlinman_core::config::WsToolConfig`] so this
/// crate doesn't depend on the gateway's runtime config plumbing — a
/// caller just destructures their config once and hands us this struct.
#[derive(Debug, Clone)]
pub struct WsToolConfig {
    pub bind: SocketAddr,
    pub auth_token: String,
    pub heartbeat_secs: u32,
    /// How many missed pings trigger disconnect. Defaults to 3.
    pub max_missed_pings: u32,
    /// Advertised server version. Purely informational at the protocol
    /// level; runners log it for debugging.
    pub server_version: String,
}

impl WsToolConfig {
    pub fn loopback(token: impl Into<String>) -> Self {
        Self {
            bind: "127.0.0.1:0".parse().expect("literal socket addr"),
            auth_token: token.into(),
            heartbeat_secs: 15,
            max_missed_pings: 3,
            server_version: env!("CARGO_PKG_VERSION").to_string(),
        }
    }
}

/// Parsed query string for `/wstool/connect`.
#[derive(Debug, Deserialize)]
struct ConnectParams {
    auth_token: String,
    runner_id: String,
    #[allow(dead_code)] // accepted for forward compat; runners send it, we log it only
    #[serde(default)]
    version: String,
}

/// Everything one connection exposes to the rest of the server.
///
/// `outbox` is the write-side mpsc (consumed by the socket-writer task).
/// `pending` correlates request_ids with their oneshot waiters; both
/// `Result` and `Error` frames for that id complete the waiter.
#[derive(Debug)]
pub(crate) struct ConnHandle {
    pub runner_id: String,
    #[allow(dead_code)]
    pub tools: Vec<ToolAdvert>,
    pub outbox: mpsc::Sender<WsToolMessage>,
    pub pending: DashMap<String, oneshot::Sender<InvokeReply>>,
}

/// Terminal outcome for one `Invoke` request_id.
#[derive(Debug)]
pub(crate) enum InvokeReply {
    Ok(serde_json::Value),
    ToolFailed {
        code: String,
        message: String,
    },
    ResultError {
        /// `ok=false` from the runner; payload is whatever error body the
        /// handler produced.
        payload: serde_json::Value,
    },
    Disconnected,
}

/// Shared server state.
///
/// Exposed publicly so adjacent modules in this crate (e.g.
/// [`crate::file_fetcher`]) can accept an `Arc<ServerState>` through
/// their constructors while remaining opaque to external callers — the
/// fields are crate-internal and external callers interact with the
/// state only via the methods on [`WsToolServer`].
pub struct ServerState {
    pub(crate) cfg: WsToolConfig,
    pub(crate) hook_bus: Arc<HookBus>,
    pub(crate) runners: DashMap<String, Arc<ConnHandle>>,
    /// tool name -> runner_id that advertised it. First wins on
    /// contention; purged on disconnect.
    pub(crate) tool_index: DashMap<String, String>,
    pub(crate) request_seq: AtomicU64,
}

impl ServerState {
    fn next_request_id(&self) -> String {
        let n = self.request_seq.fetch_add(1, Ordering::Relaxed);
        format!("req-{n}")
    }

    pub(crate) fn resolve_tool(&self, tool: &str) -> Option<Arc<ConnHandle>> {
        let runner_id = self.tool_index.get(tool)?.value().clone();
        self.runners.get(&runner_id).map(|r| r.value().clone())
    }

    pub(crate) fn make_request_id(&self) -> String {
        self.next_request_id()
    }
}

/// Public server handle.
///
/// Construct with [`WsToolServer::new`], then either call [`bind`] (which
/// spawns the axum server on `cfg.bind`) or use [`router`] to mount on an
/// existing axum app. [`runtime`] returns a [`WsToolRuntime`] that
/// implements `PluginRuntime` and resolves tools through this server.
pub struct WsToolServer {
    pub(crate) state: Arc<ServerState>,
    pub(crate) bound_addr: Mutex<Option<SocketAddr>>,
    pub(crate) join: Mutex<Option<JoinHandle<()>>>,
}

impl WsToolServer {
    pub fn new(cfg: WsToolConfig, hook_bus: Arc<HookBus>) -> Self {
        Self {
            state: Arc::new(ServerState {
                cfg,
                hook_bus,
                runners: DashMap::new(),
                tool_index: DashMap::new(),
                request_seq: AtomicU64::new(0),
            }),
            bound_addr: Mutex::new(None),
            join: Mutex::new(None),
        }
    }

    /// Build the axum router with just the `/wstool/connect` route.
    /// Useful if the gateway wants to mount us under its own app.
    pub fn router(&self) -> Router {
        Router::new()
            .route("/wstool/connect", get(ws_upgrade_handler))
            .with_state(self.state.clone())
    }

    /// Bind an axum server on `cfg.bind`. Returns once the listener is
    /// live; the server runs until the returned [`WsToolServer`] is
    /// dropped or [`shutdown`] is called.
    pub async fn bind(&self) -> anyhow::Result<SocketAddr> {
        let listener = tokio::net::TcpListener::bind(self.state.cfg.bind).await?;
        let local = listener.local_addr()?;
        let app = self.router();
        let handle = tokio::spawn(async move {
            let _ = axum::serve(
                listener,
                app.into_make_service_with_connect_info::<SocketAddr>(),
            )
            .await;
        });
        *self.bound_addr.lock().await = Some(local);
        *self.join.lock().await = Some(handle);
        info!(addr = %local, "wstool server bound");
        Ok(local)
    }

    pub async fn local_addr(&self) -> Option<SocketAddr> {
        *self.bound_addr.lock().await
    }

    /// Returns a [`WsToolRuntime`] that routes invocations through this
    /// server's connected runners. Safe to clone and reuse.
    pub fn runtime(&self) -> WsToolRuntime {
        WsToolRuntime::new(self.state.clone())
    }

    /// Abort the background listener. Idempotent.
    pub async fn shutdown(&self) {
        if let Some(h) = self.join.lock().await.take() {
            h.abort();
        }
    }

    /// Snapshot of currently-advertised tools (tool name → runner id).
    /// Intended for diagnostics and integration tests.
    pub fn advertised_tools(&self) -> HashMap<String, String> {
        advertised_tools(&self.state)
    }

    /// Number of runners currently connected.
    pub fn runner_count(&self) -> usize {
        self.state.runners.len()
    }

    /// Clone of the shared `Arc<ServerState>`. The `ServerState` type is
    /// opaque to external callers — use this handle solely to hand to
    /// modules in this crate (e.g. [`crate::file_fetcher::FileFetcher`])
    /// that need to dispatch invocations through the connected runners.
    pub fn state(&self) -> Arc<ServerState> {
        self.state.clone()
    }
}

impl Drop for WsToolServer {
    fn drop(&mut self) {
        // Best-effort abort; the JoinHandle abort is sync so we don't
        // need the async lock. We lock try_lock to avoid reentering a
        // runtime during drop.
        if let Ok(mut guard) = self.join.try_lock() {
            if let Some(h) = guard.take() {
                h.abort();
            }
        }
    }
}

async fn ws_upgrade_handler(
    ws: WebSocketUpgrade,
    Query(params): Query<ConnectParams>,
    State(state): State<Arc<ServerState>>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
) -> axum::response::Response {
    // Auth check happens pre-upgrade so we can return a plain HTTP 401
    // when the token is wrong — runners distinguish this from a
    // successful upgrade followed by a `Reject` frame.
    if params.auth_token != state.cfg.auth_token {
        warn!(%peer, runner_id = %params.runner_id, "wstool auth rejected");
        return (
            axum::http::StatusCode::UNAUTHORIZED,
            axum::Json(serde_json::json!({
                "code": "auth_rejected",
                "message": "invalid auth_token",
            })),
        )
            .into_response();
    }

    let runner_id = params.runner_id.clone();
    ws.on_upgrade(move |socket| connection_loop(socket, state, runner_id))
}

/// Core connection task: validates handshake, then multiplexes frames
/// until the socket closes or the heartbeat check fires the disconnect.
async fn connection_loop(socket: WebSocket, state: Arc<ServerState>, runner_id: String) {
    let (mut ws_tx, mut ws_rx) = socket.split();

    // Step 1: wait for the runner's first frame. It *must* be `Accept`.
    let first = match ws_rx.next().await {
        Some(Ok(WsMessage::Text(text))) => text,
        other => {
            warn!(?other, %runner_id, "wstool: missing Accept frame");
            return;
        }
    };
    let advert = match serde_json::from_str::<WsToolMessage>(&first) {
        Ok(WsToolMessage::Accept {
            supported_tools, ..
        }) => supported_tools,
        Ok(other) => {
            warn!(?other, %runner_id, "wstool: first frame was not Accept");
            return;
        }
        Err(err) => {
            warn!(%err, %runner_id, "wstool: first frame parse failed");
            return;
        }
    };

    // Step 2: register the runner + its tools in shared state.
    let (outbox_tx, mut outbox_rx) = mpsc::channel::<WsToolMessage>(64);
    let conn = Arc::new(ConnHandle {
        runner_id: runner_id.clone(),
        tools: advert.clone(),
        outbox: outbox_tx.clone(),
        pending: DashMap::new(),
    });
    state.runners.insert(runner_id.clone(), conn.clone());
    for tool in &advert {
        // First runner to advertise a tool wins.
        state
            .tool_index
            .entry(tool.name.clone())
            .or_insert_with(|| runner_id.clone());
    }
    corlinman_core::metrics::WSTOOL_RUNNERS_CONNECTED.inc();
    info!(%runner_id, tools = advert.len(), "wstool: runner connected");

    // Step 3: spawn writer task draining `outbox` to the socket.
    let writer = tokio::spawn(async move {
        while let Some(msg) = outbox_rx.recv().await {
            let text = match serde_json::to_string(&msg) {
                Ok(s) => s,
                Err(err) => {
                    warn!(%err, "wstool: serialize failed, dropping frame");
                    continue;
                }
            };
            if ws_tx.send(WsMessage::Text(text)).await.is_err() {
                break;
            }
        }
        // Drain done → close the underlying socket.
        let _ = ws_tx.close().await;
    });

    // Step 4: heartbeat task.
    let hb_state = state.clone();
    let hb_conn = conn.clone();
    let hb_runner_id = runner_id.clone();
    let missed_pings = Arc::new(std::sync::atomic::AtomicU32::new(0));
    let hb_missed = missed_pings.clone();
    let max_missed = state.cfg.max_missed_pings;
    let heartbeat_secs = state.cfg.heartbeat_secs;
    let heartbeat = tokio::spawn(async move {
        let mut ticker = tokio::time::interval(Duration::from_secs(heartbeat_secs.max(1) as u64));
        ticker.set_missed_tick_behavior(MissedTickBehavior::Delay);
        // Eat the immediate tick.
        ticker.tick().await;
        loop {
            ticker.tick().await;
            if hb_conn.outbox.send(WsToolMessage::Ping).await.is_err() {
                break;
            }
            let prior = hb_missed.fetch_add(1, Ordering::SeqCst);
            if prior + 1 >= max_missed {
                warn!(
                    runner_id = %hb_runner_id,
                    misses = prior + 1,
                    "wstool: heartbeat miss threshold hit, disconnecting"
                );
                // Drop the outbox to cascade close; clear registration.
                drop(hb_conn);
                // Safe to mutate shared state on disconnect. Only bump the
                // gauge down when we actually removed a runner — otherwise
                // the main cleanup path below would double-decrement.
                if hb_state.runners.remove(&hb_runner_id).is_some() {
                    corlinman_core::metrics::WSTOOL_RUNNERS_CONNECTED.dec();
                }
                hb_state.tool_index.retain(|_, v| v != &hb_runner_id);
                break;
            }
        }
    });

    // Step 5: reader loop — dispatch incoming frames.
    while let Some(frame) = ws_rx.next().await {
        let text = match frame {
            Ok(WsMessage::Text(t)) => t,
            Ok(WsMessage::Close(_)) => break,
            Ok(WsMessage::Ping(payload)) => {
                // Axum auto-responds to low-level pings, but explicit
                // echo is cheap and makes custom proxies happier.
                let _ = conn.outbox.send(WsToolMessage::Pong).await;
                let _ = payload;
                continue;
            }
            Ok(_) => continue,
            Err(err) => {
                debug!(%err, %runner_id, "wstool: socket read error");
                break;
            }
        };
        let msg: WsToolMessage = match serde_json::from_str(&text) {
            Ok(m) => m,
            Err(err) => {
                warn!(%err, %runner_id, "wstool: bad frame, ignoring");
                continue;
            }
        };
        handle_runner_frame(&state, &conn, &missed_pings, msg);
    }

    // Step 6: cleanup. Fail all in-flight requests with Disconnected,
    // purge registrations, join writer/heartbeat tasks.
    for entry in conn.pending.iter() {
        let request_id = entry.key().clone();
        debug!(%runner_id, %request_id, "wstool: failing pending request on disconnect");
    }
    let drained: Vec<String> = conn.pending.iter().map(|e| e.key().clone()).collect();
    for k in drained {
        if let Some((_, tx)) = conn.pending.remove(&k) {
            let _ = tx.send(InvokeReply::Disconnected);
        }
    }
    if state.runners.remove(&runner_id).is_some() {
        // Only decrement when we actually removed a runner. The heartbeat
        // path above may have already removed + decremented on a stale
        // conn.
        corlinman_core::metrics::WSTOOL_RUNNERS_CONNECTED.dec();
    }
    state.tool_index.retain(|_, v| v != &runner_id);

    // Dropping conn here frees the outbox sender; writer loop exits.
    drop(conn);
    heartbeat.abort();
    writer.abort();
    info!(%runner_id, "wstool: runner disconnected");
}

fn handle_runner_frame(
    state: &Arc<ServerState>,
    conn: &Arc<ConnHandle>,
    missed_pings: &Arc<std::sync::atomic::AtomicU32>,
    msg: WsToolMessage,
) {
    match msg {
        WsToolMessage::Pong => {
            missed_pings.store(0, Ordering::SeqCst);
        }
        WsToolMessage::Result {
            request_id,
            ok,
            payload,
        } => {
            if let Some((_, waiter)) = conn.pending.remove(&request_id) {
                let reply = if ok {
                    InvokeReply::Ok(payload)
                } else {
                    InvokeReply::ResultError { payload }
                };
                let _ = waiter.send(reply);
            }
        }
        WsToolMessage::Error {
            request_id,
            code,
            message,
        } => {
            if let Some((_, waiter)) = conn.pending.remove(&request_id) {
                let _ = waiter.send(InvokeReply::ToolFailed {
                    code: code.clone(),
                    message: message.clone(),
                });
            }
            let _ = state; // kept for symmetry — future hook emission spot
        }
        WsToolMessage::Progress { .. } => {
            // Progress is dropped for now; a future change can plumb it
            // through a per-request mpsc::Sender<serde_json::Value>.
        }
        WsToolMessage::Accept { .. } | WsToolMessage::Reject { .. } => {
            // Duplicate handshake; ignore.
        }
        WsToolMessage::Invoke { .. } | WsToolMessage::Cancel { .. } | WsToolMessage::Ping => {
            // Server-bound frames — runner violated direction. Ignore.
        }
    }
}

/// Helper used by [`crate::runtime`] to start one invocation. Lives here
/// because it manipulates `ServerState` internals.
pub(crate) async fn invoke_once(
    state: Arc<ServerState>,
    tool: String,
    args: serde_json::Value,
    timeout_ms: u64,
    cancel: tokio_util::sync::CancellationToken,
) -> Result<serde_json::Value, WsToolError> {
    let conn = state
        .resolve_tool(&tool)
        .ok_or_else(|| WsToolError::Unsupported(tool.clone()))?;

    let request_id = state.make_request_id();
    let (tx, rx) = oneshot::channel();
    conn.pending.insert(request_id.clone(), tx);

    let invoke = WsToolMessage::Invoke {
        request_id: request_id.clone(),
        tool: tool.clone(),
        args,
        timeout_ms,
    };
    if conn.outbox.send(invoke).await.is_err() {
        conn.pending.remove(&request_id);
        return Err(WsToolError::Disconnected);
    }

    let started = Instant::now();
    let runner_id = conn.runner_id.clone();
    let timeout = Duration::from_millis(timeout_ms);

    let outcome = tokio::select! {
        biased;
        _ = cancel.cancelled() => {
            conn.pending.remove(&request_id);
            let _ = conn.outbox.send(WsToolMessage::Cancel { request_id: request_id.clone() }).await;
            emit_tool_called(&state, &tool, &runner_id, started.elapsed().as_millis() as u64, false, Some("cancelled")).await;
            return Err(WsToolError::ToolFailed {
                code: "cancelled".into(),
                message: "caller cancelled".into(),
            });
        }
        res = tokio::time::timeout(timeout, rx) => res,
    };

    let duration_ms = started.elapsed().as_millis() as u64;
    match outcome {
        Ok(Ok(InvokeReply::Ok(val))) => {
            emit_tool_called(&state, &tool, &runner_id, duration_ms, true, None).await;
            Ok(val)
        }
        Ok(Ok(InvokeReply::ToolFailed { code, message })) => {
            emit_tool_called(&state, &tool, &runner_id, duration_ms, false, Some(&code)).await;
            Err(WsToolError::ToolFailed { code, message })
        }
        Ok(Ok(InvokeReply::ResultError { payload })) => {
            emit_tool_called(
                &state,
                &tool,
                &runner_id,
                duration_ms,
                false,
                Some("result_error"),
            )
            .await;
            Err(WsToolError::ToolFailed {
                code: "result_error".into(),
                message: payload.to_string(),
            })
        }
        Ok(Ok(InvokeReply::Disconnected)) => {
            emit_tool_called(
                &state,
                &tool,
                &runner_id,
                duration_ms,
                false,
                Some("disconnected"),
            )
            .await;
            Err(WsToolError::Disconnected)
        }
        Ok(Err(_)) => {
            // oneshot dropped without reply; treat as disconnect.
            emit_tool_called(
                &state,
                &tool,
                &runner_id,
                duration_ms,
                false,
                Some("disconnected"),
            )
            .await;
            Err(WsToolError::Disconnected)
        }
        Err(_) => {
            conn.pending.remove(&request_id);
            let _ = conn
                .outbox
                .send(WsToolMessage::Cancel {
                    request_id: request_id.clone(),
                })
                .await;
            emit_tool_called(
                &state,
                &tool,
                &runner_id,
                timeout_ms,
                false,
                Some("timeout"),
            )
            .await;
            Err(WsToolError::Timeout { millis: timeout_ms })
        }
    }
}

async fn emit_tool_called(
    state: &ServerState,
    tool: &str,
    runner_id: &str,
    duration_ms: u64,
    ok: bool,
    error_code: Option<&str>,
) {
    let event = HookEvent::ToolCalled {
        tool: tool.to_string(),
        runner_id: runner_id.to_string(),
        duration_ms,
        ok,
        error_code: error_code.map(|s| s.to_string()),
        // Phase 4 W1.5 (next-tasks A1): wstool runs out of band
        // from the gateway's tenant middleware, so it has no
        // tenant context to propagate. Observer falls back to
        // "default".
        tenant_id: None,
    };
    let _ = state.hook_bus.emit(event).await;
}

/// Exposed for crate-internal tests — quickly list currently-advertised
/// tools (map of tool name → runner id).
pub(crate) fn advertised_tools(state: &ServerState) -> HashMap<String, String> {
    let mut map = HashMap::new();
    for entry in state.tool_index.iter() {
        map.insert(entry.key().clone(), entry.value().clone());
    }
    map
}
