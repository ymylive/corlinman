//! WebSocket transport for the `/mcp` endpoint.
//!
//! Modelled on `corlinman-wstool/src/server.rs:251-273`: the auth gate
//! lives **pre-upgrade** so a wrong / missing token surfaces as plain
//! HTTP 401 (no WS upgrade). Successful upgrades enter
//! [`connection_loop`], which reads JSON-RPC frames off the socket and
//! dispatches them through a [`FrameHandler`] supplied by the caller.
//!
//! Iter 4 ships only the transport plumbing: the default handler used
//! by the unit tests is a stub that always returns
//! [`McpError::MethodNotFound`]. Iter 5 swaps in the real
//! adapter-backed dispatcher; the transport contract here doesn't
//! need to change for that.
//!
//! Per-connection state owned by the loop:
//!   - `SessionState` — handshake gate; iter 4 doesn't itself enforce
//!     the gate (the stub handler ignores the state), but it's
//!     threaded through so iter 5 can flip it on without churn.
//!   - `max_frame_bytes` — frames larger than this trigger a WS close
//!     with code 1009 (Message Too Big) per RFC 6455 §7.4.1.

use std::net::SocketAddr;
use std::sync::Arc;

use async_trait::async_trait;
use axum::extract::ws::{CloseFrame, Message as WsMessage, WebSocket, WebSocketUpgrade};
use axum::extract::{ConnectInfo, Query, State};
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::routing::get;
use axum::Router;
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use serde_json::Value as JsonValue;
use tokio::sync::Mutex;
use tracing::{debug, info, warn};

use crate::adapters::SessionContext;
use crate::error::McpError;
use crate::schema::{JsonRpcRequest, JsonRpcResponse};
use crate::server::auth::{resolve_token, TokenAcl};
use crate::server::session::SessionState;

/// WebSocket close code for "Message Too Big" (RFC 6455 §7.4.1).
pub const CLOSE_CODE_MESSAGE_TOO_BIG: u16 = 1009;

/// Default cap on a single inbound frame. Mirrors the design's
/// `max_frame_bytes` knob (1 MiB).
pub const DEFAULT_MAX_FRAME_BYTES: usize = 1_048_576;

/// Configuration for [`McpServer`]. Kept independent of
/// `corlinman-core`'s `McpConfig` so this crate can be wired against
/// either the gateway runtime config or hand-built test fixtures.
#[derive(Debug, Clone)]
pub struct McpServerConfig {
    /// Accepted bearer-token ACLs. **Empty list = reject everything**
    /// (fail-closed posture mirrors `meta_approver_users = []`).
    /// Each ACL pins per-capability allowlists + tenant scope; iter 8
    /// upgraded the field from a flat `Vec<String>` so the transport's
    /// pre-upgrade gate and the per-call adapter context resolve from
    /// one source of truth.
    pub tokens: Vec<TokenAcl>,
    /// Per-frame size limit. Inbound frames over this trigger a
    /// 1009 close.
    pub max_frame_bytes: usize,
}

impl McpServerConfig {
    /// Convenience: a single permissive token, default frame cap.
    /// Used by tests; production tokens should narrow the ACL.
    pub fn with_token(token: impl Into<String>) -> Self {
        Self {
            tokens: vec![TokenAcl::permissive(token)],
            max_frame_bytes: DEFAULT_MAX_FRAME_BYTES,
        }
    }

    /// Convenience: a single fully-pinned ACL.
    pub fn with_acl(acl: TokenAcl) -> Self {
        Self {
            tokens: vec![acl],
            max_frame_bytes: DEFAULT_MAX_FRAME_BYTES,
        }
    }
}

impl Default for McpServerConfig {
    fn default() -> Self {
        Self {
            tokens: Vec::new(),
            max_frame_bytes: DEFAULT_MAX_FRAME_BYTES,
        }
    }
}

/// Pluggable JSON-RPC frame handler. Iter 4's default is the stub in
/// [`StubMethodNotFoundHandler`]; iter 5 supplies an adapter-backed
/// implementation.
///
/// Iter 8 widens the signature with the per-connection
/// [`SessionContext`] derived from the resolved [`TokenAcl`]. Handlers
/// pass the context straight to their adapters so allowlist + tenant
/// scoping fire on every method call.
#[async_trait]
pub trait FrameHandler: Send + Sync + 'static {
    /// Handle one inbound JSON-RPC request. The handler MUST inspect
    /// `req.is_notification()` and either:
    ///   - return `Ok(None)` for notifications (no reply written), or
    ///   - return `Ok(Some(JsonRpcResponse))` for a request reply, or
    ///   - return `Err(McpError)` to let the transport lift it onto a
    ///     JSON-RPC error frame using the request id.
    async fn handle(
        &self,
        req: JsonRpcRequest,
        session: &Mutex<SessionState>,
        ctx: &SessionContext,
    ) -> Result<Option<JsonRpcResponse>, McpError>;
}

/// Default handler used by iter 4's tests: ignores `session` and `ctx`,
/// returns `MethodNotFound` for every request, drops every notification.
pub struct StubMethodNotFoundHandler;

#[async_trait]
impl FrameHandler for StubMethodNotFoundHandler {
    async fn handle(
        &self,
        req: JsonRpcRequest,
        _session: &Mutex<SessionState>,
        _ctx: &SessionContext,
    ) -> Result<Option<JsonRpcResponse>, McpError> {
        if req.is_notification() {
            return Ok(None);
        }
        Err(McpError::MethodNotFound(req.method))
    }
}

/// Public server handle. Construct with [`McpServer::new`], then call
/// [`router`] to mount on an existing axum app or [`bind`] to spawn a
/// listener.
pub struct McpServer {
    state: Arc<ServerState>,
}

struct ServerState {
    cfg: McpServerConfig,
    handler: Arc<dyn FrameHandler>,
}

impl McpServer {
    pub fn new(cfg: McpServerConfig, handler: Arc<dyn FrameHandler>) -> Self {
        Self {
            state: Arc::new(ServerState { cfg, handler }),
        }
    }

    /// Convenience: construct with the iter-4 stub handler.
    pub fn with_stub(cfg: McpServerConfig) -> Self {
        Self::new(cfg, Arc::new(StubMethodNotFoundHandler))
    }

    /// Build the axum router with the `/mcp` route.
    pub fn router(&self) -> Router {
        Router::new()
            .route("/mcp", get(ws_upgrade_handler))
            .with_state(self.state.clone())
    }
}

/// Parsed query string for `GET /mcp`. Only `token` is meaningful in
/// iter 4; iter 8 will widen with optional `client_id` and `tenant`.
#[derive(Debug, Deserialize)]
struct ConnectParams {
    #[serde(default)]
    token: Option<String>,
}

async fn ws_upgrade_handler(
    ws: WebSocketUpgrade,
    Query(params): Query<ConnectParams>,
    State(state): State<Arc<ServerState>>,
    peer: Option<ConnectInfo<SocketAddr>>,
) -> axum::response::Response {
    let token = match params.token {
        Some(t) if !t.is_empty() => t,
        _ => {
            warn!(?peer, "mcp: missing/empty token; rejecting pre-upgrade");
            return reject_unauthorized("missing token");
        }
    };
    let acl = match resolve_token(&state.cfg.tokens, &token) {
        Some(acl) => acl.clone(),
        None => {
            warn!(?peer, "mcp: invalid token; rejecting pre-upgrade");
            return reject_unauthorized("invalid token");
        }
    };
    info!(
        ?peer,
        label = %acl.label,
        tenant = %acl.effective_tenant(),
        "mcp: token resolved; upgrading"
    );
    let state2 = state.clone();
    ws.on_upgrade(move |socket| connection_loop(socket, state2, acl))
}

fn reject_unauthorized(message: &str) -> axum::response::Response {
    (
        StatusCode::UNAUTHORIZED,
        axum::Json(serde_json::json!({
            "code": "auth_rejected",
            "message": message,
        })),
    )
        .into_response()
}

/// Per-connection reader/writer loop. One WebSocket = one
/// [`SessionState`] + one [`SessionContext`] (derived from the resolved
/// [`TokenAcl`] at pre-upgrade). Inbound frames go to the handler with
/// both pinned; the handler's reply (or a lifted error) goes back out on
/// the socket.
async fn connection_loop(socket: WebSocket, state: Arc<ServerState>, acl: TokenAcl) {
    let (mut ws_tx, mut ws_rx) = socket.split();
    let session = Mutex::new(SessionState::new());
    let ctx = acl.to_session_context();
    info!(label = %acl.label, "mcp: connection opened");

    while let Some(frame) = ws_rx.next().await {
        let text = match frame {
            Ok(WsMessage::Text(t)) => t,
            Ok(WsMessage::Binary(_)) => {
                warn!("mcp: binary frame rejected; MCP is text-only");
                continue;
            }
            Ok(WsMessage::Ping(_)) | Ok(WsMessage::Pong(_)) => continue,
            Ok(WsMessage::Close(_)) => {
                debug!("mcp: client closed");
                break;
            }
            Err(err) => {
                warn!(%err, "mcp: ws read error");
                break;
            }
        };

        // Frame-size gate. Strings bigger than the cap → WS close 1009.
        // Per RFC 6455 §7.4.1 the close payload is the close code +
        // optional reason string.
        if text.len() > state.cfg.max_frame_bytes {
            warn!(
                size = text.len(),
                cap = state.cfg.max_frame_bytes,
                "mcp: oversize frame; closing with 1009"
            );
            let _ = ws_tx
                .send(WsMessage::Close(Some(CloseFrame {
                    code: CLOSE_CODE_MESSAGE_TOO_BIG,
                    reason: "frame too large".into(),
                })))
                .await;
            return;
        }

        // Parse the envelope. Bad JSON → JSON-RPC parse error with
        // `id: null` (per spec the id is unknown when parsing fails).
        let req = match serde_json::from_str::<JsonRpcRequest>(&text) {
            Ok(r) => r,
            Err(err) => {
                let resp = JsonRpcResponse::err(
                    JsonValue::Null,
                    McpError::ParseError(err.to_string()).into(),
                );
                let _ = send_json(&mut ws_tx, &resp).await;
                continue;
            }
        };

        let id = req.id.clone();
        let is_notif = req.is_notification();
        match state.handler.handle(req, &session, &ctx).await {
            Ok(Some(resp)) => {
                if let Err(err) = send_json(&mut ws_tx, &resp).await {
                    warn!(%err, "mcp: write failed; tearing down");
                    break;
                }
            }
            Ok(None) => {
                // Handler chose not to reply (notifications).
            }
            Err(err) => {
                if is_notif {
                    // Notifications never get error replies on the
                    // wire (JSON-RPC §4.1).
                    debug!(%err, "mcp: notification handler errored; suppressed per spec");
                    continue;
                }
                let resp = JsonRpcResponse::err(id.unwrap_or(JsonValue::Null), err.into());
                if let Err(err) = send_json(&mut ws_tx, &resp).await {
                    warn!(%err, "mcp: write failed; tearing down");
                    break;
                }
            }
        }
    }
    info!("mcp: connection closed");
}

async fn send_json<S>(sink: &mut S, resp: &JsonRpcResponse) -> Result<(), McpError>
where
    S: SinkExt<WsMessage, Error = axum::Error> + Unpin,
{
    let text = serde_json::to_string(resp)
        .map_err(|e| McpError::Internal(format!("response serialize: {e}")))?;
    sink.send(WsMessage::Text(text))
        .await
        .map_err(|e| McpError::Transport(e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::{error_codes, JsonRpcRequest, JsonRpcResponse};
    use futures_util::{SinkExt, StreamExt};
    use std::net::SocketAddr;
    use tokio::net::TcpListener;
    use tokio_tungstenite::tungstenite::Message as TgMessage;

    /// Spin up an isolated MCP server on `127.0.0.1:0`, return the bound
    /// address + a shutdown handle. Mirrors wstool's `Harness::new`.
    async fn spawn(cfg: McpServerConfig) -> (SocketAddr, tokio::task::JoinHandle<()>) {
        let server = McpServer::with_stub(cfg);
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let app = server
            .router()
            .into_make_service_with_connect_info::<SocketAddr>();
        let handle = tokio::spawn(async move {
            let _ = axum::serve(listener, app).await;
        });
        // Yield once so the listener is definitely accept()-ing before
        // the test dials. No sleep, no flake.
        tokio::task::yield_now().await;
        (addr, handle)
    }

    #[tokio::test]
    async fn rejects_pre_upgrade_when_token_missing() {
        let (addr, _h) = spawn(McpServerConfig::with_token("good")).await;
        let url = format!("ws://{addr}/mcp"); // no ?token=
        let err = tokio_tungstenite::connect_async(&url)
            .await
            .expect_err("must reject");
        let msg = err.to_string();
        assert!(
            msg.contains("401") || msg.to_lowercase().contains("unauthorized"),
            "expected 401, got {msg}"
        );
    }

    /// Iter 8: a structured ACL token must be accepted at pre-upgrade
    /// just like a permissive one, *and* the connection's resolved
    /// `SessionContext` must carry the ACL's tenant + allowlists onto
    /// every handler call.
    #[tokio::test]
    async fn structured_acl_resolves_through_pre_upgrade() {
        let acl = TokenAcl {
            token: "acl-token".into(),
            label: "scoped-laptop".into(),
            tools_allowlist: vec!["kb:*".into()],
            resources_allowed: vec!["skill".into()],
            prompts_allowed: vec!["*".into()],
            tenant_id: Some("alpha".into()),
        };
        let (addr, _h) = spawn(McpServerConfig::with_acl(acl)).await;
        let url = format!("ws://{addr}/mcp?token=acl-token");
        let (mut ws, resp) = tokio_tungstenite::connect_async(&url).await.unwrap();
        assert_eq!(resp.status().as_u16(), 101, "must upgrade");

        // Stub handler still returns MethodNotFound — the goal is to
        // confirm the upgrade path doesn't drop a structured ACL.
        let req = JsonRpcRequest {
            jsonrpc: "2.0".into(),
            id: Some(serde_json::json!("p")),
            method: "tools/list".into(),
            params: serde_json::Value::Null,
        };
        ws.send(TgMessage::Text(serde_json::to_string(&req).unwrap()))
            .await
            .unwrap();
        let reply = ws.next().await.expect("reply").expect("ok");
        match reply {
            TgMessage::Text(t) => {
                let parsed: JsonRpcResponse = serde_json::from_str(&t).unwrap();
                match parsed {
                    JsonRpcResponse::Error { error, .. } => {
                        // Stub still returns MethodNotFound; the
                        // handler-side ACL plumbing is exercised in the
                        // dedicated `tests/auth_acl.rs` integration test.
                        assert_eq!(error.code, error_codes::METHOD_NOT_FOUND);
                    }
                    other => panic!("expected error, got {other:?}"),
                }
            }
            other => panic!("expected text, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn rejects_pre_upgrade_when_token_wrong() {
        let (addr, _h) = spawn(McpServerConfig::with_token("good")).await;
        let url = format!("ws://{addr}/mcp?token=BAD");
        let err = tokio_tungstenite::connect_async(&url)
            .await
            .expect_err("must reject");
        let msg = err.to_string();
        assert!(
            msg.contains("401") || msg.to_lowercase().contains("unauthorized"),
            "expected 401, got {msg}"
        );
    }

    #[tokio::test]
    async fn empty_token_list_rejects_everything() {
        let cfg = McpServerConfig::default(); // empty tokens
        let (addr, _h) = spawn(cfg).await;
        let url = format!("ws://{addr}/mcp?token=anything");
        let err = tokio_tungstenite::connect_async(&url)
            .await
            .expect_err("fail-closed default must reject");
        let msg = err.to_string();
        assert!(msg.contains("401") || msg.to_lowercase().contains("unauthorized"));
    }

    #[tokio::test]
    async fn upgrades_with_valid_token_and_stub_returns_method_not_found() {
        let (addr, _h) = spawn(McpServerConfig::with_token("good")).await;
        let url = format!("ws://{addr}/mcp?token=good");
        let (mut ws, resp) = tokio_tungstenite::connect_async(&url).await.unwrap();
        assert_eq!(resp.status().as_u16(), 101, "must upgrade");

        // Send a request; stub handler returns MethodNotFound.
        let req = JsonRpcRequest {
            jsonrpc: "2.0".into(),
            id: Some(serde_json::json!("req-1")),
            method: "tools/list".into(),
            params: serde_json::Value::Null,
        };
        ws.send(TgMessage::Text(serde_json::to_string(&req).unwrap()))
            .await
            .unwrap();

        let reply = ws.next().await.expect("reply").expect("ok");
        let text = match reply {
            TgMessage::Text(t) => t,
            other => panic!("unexpected frame: {other:?}"),
        };
        let parsed: JsonRpcResponse = serde_json::from_str(&text).unwrap();
        match parsed {
            JsonRpcResponse::Error { id, error, .. } => {
                assert_eq!(id, serde_json::json!("req-1"));
                assert_eq!(error.code, error_codes::METHOD_NOT_FOUND);
                assert!(error.message.contains("tools/list"));
            }
            other => panic!("expected error frame, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn notifications_get_no_reply() {
        let (addr, _h) = spawn(McpServerConfig::with_token("good")).await;
        let url = format!("ws://{addr}/mcp?token=good");
        let (mut ws, _resp) = tokio_tungstenite::connect_async(&url).await.unwrap();

        let notif = JsonRpcRequest {
            jsonrpc: "2.0".into(),
            id: None, // notification
            method: "notifications/initialized".into(),
            params: serde_json::Value::Null,
        };
        ws.send(TgMessage::Text(serde_json::to_string(&notif).unwrap()))
            .await
            .unwrap();

        // Now send a real request to prove the conn is still alive AND
        // that we didn't get a stray reply queued before it.
        let req = JsonRpcRequest {
            jsonrpc: "2.0".into(),
            id: Some(serde_json::json!("after-notif")),
            method: "tools/list".into(),
            params: serde_json::Value::Null,
        };
        ws.send(TgMessage::Text(serde_json::to_string(&req).unwrap()))
            .await
            .unwrap();
        let reply = ws.next().await.expect("reply").expect("ok");
        let text = match reply {
            TgMessage::Text(t) => t,
            other => panic!("unexpected frame: {other:?}"),
        };
        let parsed: JsonRpcResponse = serde_json::from_str(&text).unwrap();
        // Must be the reply to "after-notif", not to the notification.
        assert_eq!(parsed.id(), &serde_json::json!("after-notif"));
    }

    #[tokio::test]
    async fn malformed_json_replies_with_parse_error_and_null_id() {
        let (addr, _h) = spawn(McpServerConfig::with_token("good")).await;
        let url = format!("ws://{addr}/mcp?token=good");
        let (mut ws, _resp) = tokio_tungstenite::connect_async(&url).await.unwrap();

        ws.send(TgMessage::Text("{not json".into())).await.unwrap();
        let reply = ws.next().await.expect("reply").expect("ok");
        let text = match reply {
            TgMessage::Text(t) => t,
            other => panic!("unexpected frame: {other:?}"),
        };
        let parsed: JsonRpcResponse = serde_json::from_str(&text).unwrap();
        match parsed {
            JsonRpcResponse::Error { id, error, .. } => {
                assert!(id.is_null(), "id must be null when parse fails");
                assert_eq!(error.code, error_codes::PARSE_ERROR);
            }
            other => panic!("expected error frame, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn oversize_frame_triggers_close_1009() {
        let mut cfg = McpServerConfig::with_token("good");
        cfg.max_frame_bytes = 256;
        let (addr, _h) = spawn(cfg).await;
        let url = format!("ws://{addr}/mcp?token=good");
        let (mut ws, _resp) = tokio_tungstenite::connect_async(&url).await.unwrap();

        // > 256 bytes
        let huge: String = "x".repeat(1024);
        ws.send(TgMessage::Text(huge)).await.unwrap();

        // Expect a Close frame with code 1009.
        let reply = ws.next().await.expect("frame").expect("ok");
        match reply {
            TgMessage::Close(Some(cf)) => {
                assert_eq!(u16::from(cf.code), CLOSE_CODE_MESSAGE_TOO_BIG);
            }
            other => panic!("expected close 1009, got {other:?}"),
        }
    }
}
