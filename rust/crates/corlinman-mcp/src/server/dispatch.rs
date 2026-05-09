//! Real `FrameHandler` implementation that drives the per-session
//! state machine and dispatches by method-prefix to a set of registered
//! [`CapabilityAdapter`]s.
//!
//! Iter 4 shipped a stub handler that always returned
//! `MethodNotFound`. Iter 9 lands the actual dispatcher so the gateway
//! integration can mount a working `/mcp` endpoint.
//!
//! ## Routing
//!
//! Method dispatch happens in three stages:
//!
//! 1. **Lifecycle gate.** [`SessionState::check_request_allowed`] /
//!    [`SessionState::check_notification_allowed`] (iter 3) refuses
//!    non-`initialize` requests while the session is still in
//!    `Connected` / `Initializing`.
//! 2. **Built-in methods.** `initialize` and
//!    `notifications/initialized` are handled here, not by an adapter:
//!    they mutate `SessionState` and emit the canonical
//!    [`InitializeResult`] reply.
//! 3. **Capability adapters.** Anything else is routed by the prefix
//!    before the first `/`. `tools/list` → the adapter whose
//!    `capability_name() == "tools"`. Adapters are stored in a
//!    `BTreeMap<&'static str, Arc<dyn CapabilityAdapter>>` so lookup
//!    is O(log n) and order is stable for snapshot tests.

use std::collections::BTreeMap;
use std::sync::Arc;

use async_trait::async_trait;
use serde_json::Value as JsonValue;
use tokio::sync::Mutex;
use tracing::{debug, warn};

use crate::adapters::{CapabilityAdapter, SessionContext};
use crate::error::McpError;
use crate::schema::{
    InitializeParams, JsonRpcRequest, JsonRpcResponse, ServerCapabilities,
};
use crate::server::session::{
    initialize_reply, SessionState, INITIALIZED_NOTIFICATION, INITIALIZE_METHOD,
};
use crate::server::transport::FrameHandler;

/// Server identity surfaced in `initialize` replies.
#[derive(Debug, Clone)]
pub struct ServerInfo {
    pub name: String,
    pub version: String,
}

impl Default for ServerInfo {
    fn default() -> Self {
        Self {
            name: "corlinman".into(),
            version: env!("CARGO_PKG_VERSION").into(),
        }
    }
}

/// Real `FrameHandler` — built from a set of capability adapters. The
/// transport's per-connection `SessionState` + `SessionContext` are
/// supplied at call time; this handler owns no per-connection state.
pub struct AdapterDispatcher {
    adapters: BTreeMap<&'static str, Arc<dyn CapabilityAdapter>>,
    server_info: ServerInfo,
    /// Server-side advertised capabilities for the `initialize` reply.
    /// Built from the adapter set at construction so adding a tools
    /// adapter automatically advertises `tools: {}`.
    capabilities: ServerCapabilities,
}

impl AdapterDispatcher {
    /// Build with no adapters; subsequent `register` calls add them.
    /// Used by tests; production callers should prefer
    /// [`AdapterDispatcher::from_adapters`].
    pub fn new(server_info: ServerInfo) -> Self {
        Self {
            adapters: BTreeMap::new(),
            server_info,
            capabilities: ServerCapabilities::default(),
        }
    }

    /// Build from a set of adapters; advertised capabilities are
    /// derived from the registered names.
    pub fn from_adapters(
        server_info: ServerInfo,
        adapters: Vec<Arc<dyn CapabilityAdapter>>,
    ) -> Self {
        let mut d = Self::new(server_info);
        for a in adapters {
            d.register(a);
        }
        d
    }

    /// Register one capability adapter. Last-write-wins on duplicates;
    /// the dispatcher logs a warning so a typo doesn't silently
    /// shadow.
    pub fn register(&mut self, adapter: Arc<dyn CapabilityAdapter>) {
        let cap = adapter.capability_name();
        if self.adapters.contains_key(cap) {
            warn!(capability = cap, "mcp dispatcher: duplicate adapter; replacing");
        }
        self.adapters.insert(cap, adapter);
        // Refresh advertised capabilities. We use the spec's "object,
        // even if empty" shape — present means supported.
        match cap {
            "tools" => {
                self.capabilities.tools = Some(crate::schema::ToolsCapability::default());
            }
            "resources" => {
                self.capabilities.resources = Some(
                    crate::schema::ResourcesCapability {
                        // C1 advertises subscribe=false (per design Open Q §3).
                        subscribe: Some(false),
                        list_changed: None,
                    },
                );
            }
            "prompts" => {
                self.capabilities.prompts = Some(crate::schema::PromptsCapability::default());
            }
            other => {
                warn!(capability = %other, "mcp dispatcher: unknown adapter capability");
            }
        }
    }

    fn capability_for(method: &str) -> Option<&'static str> {
        let prefix = method.split('/').next()?;
        match prefix {
            "tools" => Some("tools"),
            "resources" => Some("resources"),
            "prompts" => Some("prompts"),
            _ => None,
        }
    }
}

#[async_trait]
impl FrameHandler for AdapterDispatcher {
    async fn handle(
        &self,
        req: JsonRpcRequest,
        session: &Mutex<SessionState>,
        ctx: &SessionContext,
    ) -> Result<Option<JsonRpcResponse>, McpError> {
        // Notifications are gated separately — they never produce a
        // reply on the wire, even on error.
        if req.is_notification() {
            // Lifecycle gate.
            {
                let s = session.lock().await;
                if let Err(err) = s.check_notification_allowed(&req.method) {
                    debug!(method = %req.method, %err, "mcp: notification rejected by lifecycle gate");
                    return Ok(None);
                }
            }
            if req.method == INITIALIZED_NOTIFICATION {
                let mut s = session.lock().await;
                let _ = s.observe_initialized_notification();
            }
            // Other notifications are accepted but no-op in C1.
            return Ok(None);
        }

        let id = req.id.clone().unwrap_or(JsonValue::Null);

        // Lifecycle gate (requests).
        {
            let s = session.lock().await;
            if let Err(err) = s.check_request_allowed(&req.method) {
                return Err(err);
            }
        }

        // Built-in `initialize` reply.
        if req.method == INITIALIZE_METHOD {
            let parsed: InitializeParams =
                serde_json::from_value(req.params.clone()).map_err(|e| {
                    McpError::invalid_params(format!("initialize: bad params: {e}"))
                })?;
            {
                let mut s = session.lock().await;
                s.observe_initialize(&parsed)?;
            }
            let reply = initialize_reply(
                self.capabilities.clone(),
                self.server_info.name.clone(),
                self.server_info.version.clone(),
            );
            let value = serde_json::to_value(reply).map_err(|e| {
                McpError::Internal(format!("initialize: serialize reply: {e}"))
            })?;
            return Ok(Some(JsonRpcResponse::ok(id, value)));
        }

        // Capability dispatch.
        let cap = match Self::capability_for(&req.method) {
            Some(c) => c,
            None => return Err(McpError::MethodNotFound(req.method)),
        };
        let adapter = match self.adapters.get(cap) {
            Some(a) => a.clone(),
            None => return Err(McpError::MethodNotFound(req.method)),
        };
        let result_value = adapter.handle(&req.method, req.params, ctx).await?;
        Ok(Some(JsonRpcResponse::ok(id, result_value)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::adapters::CapabilityAdapter;
    use crate::schema::JsonRpcResponse;
    use serde_json::json;

    /// Dummy adapter used to exercise dispatch routing without dragging
    /// in plugin / memory / skill state.
    struct DummyAdapter {
        name: &'static str,
        last_method: std::sync::Mutex<Option<String>>,
    }

    #[async_trait]
    impl CapabilityAdapter for DummyAdapter {
        fn capability_name(&self) -> &'static str {
            self.name
        }
        async fn handle(
            &self,
            method: &str,
            _params: JsonValue,
            _ctx: &SessionContext,
        ) -> Result<JsonValue, McpError> {
            *self.last_method.lock().unwrap() = Some(method.to_string());
            Ok(json!({"adapter": self.name, "method": method}))
        }
    }

    fn dummy(name: &'static str) -> Arc<DummyAdapter> {
        Arc::new(DummyAdapter {
            name,
            last_method: std::sync::Mutex::new(None),
        })
    }

    #[tokio::test]
    async fn initialize_advertises_registered_capabilities_only() {
        let d = AdapterDispatcher::from_adapters(
            ServerInfo::default(),
            vec![dummy("tools") as Arc<dyn CapabilityAdapter>],
        );
        let session = Mutex::new(SessionState::new());
        let ctx = SessionContext::permissive();

        let req = JsonRpcRequest {
            jsonrpc: "2.0".into(),
            id: Some(json!("init-1")),
            method: "initialize".into(),
            params: json!({
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0.1"}
            }),
        };
        let reply = d.handle(req, &session, &ctx).await.unwrap().expect("reply");
        match reply {
            JsonRpcResponse::Result { result, .. } => {
                assert_eq!(result["capabilities"]["tools"], json!({}));
                assert!(result["capabilities"]["resources"].is_null());
                assert!(result["capabilities"]["prompts"].is_null());
                assert_eq!(result["protocolVersion"], "2024-11-05");
                assert_eq!(result["serverInfo"]["name"], "corlinman");
            }
            other => panic!("expected Result, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn pre_initialize_request_returns_session_not_initialized() {
        let d = AdapterDispatcher::from_adapters(
            ServerInfo::default(),
            vec![dummy("tools") as Arc<dyn CapabilityAdapter>],
        );
        let session = Mutex::new(SessionState::new());
        let ctx = SessionContext::permissive();

        let req = JsonRpcRequest {
            jsonrpc: "2.0".into(),
            id: Some(json!(1)),
            method: "tools/list".into(),
            params: JsonValue::Null,
        };
        let err = d.handle(req, &session, &ctx).await.expect_err("must err");
        assert_eq!(err.jsonrpc_code(), -32002); // SESSION_NOT_INITIALIZED
    }

    #[tokio::test]
    async fn post_handshake_dispatch_routes_to_capability_adapter() {
        let tools = dummy("tools");
        let d = AdapterDispatcher::from_adapters(
            ServerInfo::default(),
            vec![tools.clone() as Arc<dyn CapabilityAdapter>],
        );
        let session = Mutex::new(SessionState::new());
        let ctx = SessionContext::permissive();

        // Walk the handshake first.
        let init_req = JsonRpcRequest {
            jsonrpc: "2.0".into(),
            id: Some(json!("i")),
            method: "initialize".into(),
            params: json!({
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"}
            }),
        };
        let _ = d.handle(init_req, &session, &ctx).await.unwrap().unwrap();
        let initd = JsonRpcRequest {
            jsonrpc: "2.0".into(),
            id: None,
            method: "notifications/initialized".into(),
            params: JsonValue::Null,
        };
        let _ = d.handle(initd, &session, &ctx).await.unwrap();

        // Now dispatch.
        let req = JsonRpcRequest {
            jsonrpc: "2.0".into(),
            id: Some(json!(7)),
            method: "tools/list".into(),
            params: JsonValue::Null,
        };
        let reply = d.handle(req, &session, &ctx).await.unwrap().expect("reply");
        match reply {
            JsonRpcResponse::Result { id, result, .. } => {
                assert_eq!(id, json!(7));
                assert_eq!(result["adapter"], "tools");
                assert_eq!(result["method"], "tools/list");
            }
            other => panic!("expected Result, got {other:?}"),
        }
        assert_eq!(
            tools.last_method.lock().unwrap().as_deref(),
            Some("tools/list")
        );
    }

    #[tokio::test]
    async fn unknown_capability_returns_method_not_found() {
        let d = AdapterDispatcher::from_adapters(
            ServerInfo::default(),
            vec![dummy("tools") as Arc<dyn CapabilityAdapter>],
        );
        let session = Mutex::new(SessionState::new());
        let ctx = SessionContext::permissive();

        // Walk handshake.
        let _ = d
            .handle(
                JsonRpcRequest {
                    jsonrpc: "2.0".into(),
                    id: Some(json!("i")),
                    method: "initialize".into(),
                    params: json!({
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "0"}
                    }),
                },
                &session,
                &ctx,
            )
            .await
            .unwrap();
        let _ = d
            .handle(
                JsonRpcRequest {
                    jsonrpc: "2.0".into(),
                    id: None,
                    method: "notifications/initialized".into(),
                    params: JsonValue::Null,
                },
                &session,
                &ctx,
            )
            .await
            .unwrap();

        // resources/list with no resources adapter registered.
        let err = d
            .handle(
                JsonRpcRequest {
                    jsonrpc: "2.0".into(),
                    id: Some(json!(1)),
                    method: "resources/list".into(),
                    params: JsonValue::Null,
                },
                &session,
                &ctx,
            )
            .await
            .expect_err("must err");
        assert_eq!(err.jsonrpc_code(), -32601);
    }
}
