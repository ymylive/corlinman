//! Crate-level error type and `JsonRpcError` mapping.
//!
//! [`McpError`] is the internal `Result` payload throughout the crate.
//! It carries enough taxonomy for the dispatcher to map onto JSON-RPC
//! 2.0 error codes (§5.1) plus the corlinman-extension codes pinned in
//! [`crate::schema::error_codes`].
//!
//! Mapping table (also exercised in unit tests below):
//!
//! | `McpError`              | JSON-RPC code | Spec / origin                      |
//! |-------------------------|---------------|------------------------------------|
//! | `Transport`             | -32603        | Internal error (transport-level)   |
//! | `Auth`                  | -32603        | Auth lives pre-upgrade; surfaces   |
//! |                         |               | here only for in-band capability   |
//! |                         |               | denials (mapped to internal).      |
//! | `SessionNotInitialized` | -32002        | corlinman extension                |
//! | `ToolNotAllowed`        | -32001        | corlinman extension                |
//! | `MethodNotFound`        | -32601        | JSON-RPC §5.1                      |
//! | `InvalidParams`         | -32602        | JSON-RPC §5.1                      |
//! | `InvalidRequest`        | -32600        | JSON-RPC §5.1                      |
//! | `ParseError`            | -32700        | JSON-RPC §5.1                      |
//! | `Internal`              | -32603        | JSON-RPC §5.1                      |
//!
//! `Auth` rejection at the WebSocket-upgrade boundary is HTTP 401,
//! never a JSON-RPC frame — see iter 4. `Auth` lands in this enum
//! only for in-band ACL denials that *aren't* tool-allowlist (e.g.
//! resource-scheme mismatch); those surface as `Internal` so we do
//! not leak token shape over the wire.

use serde_json::Value as JsonValue;
use thiserror::Error;

use crate::schema::{error_codes, JsonRpcError};

/// Crate-level error. `Result<T, McpError>` is the canonical return
/// shape inside the dispatcher and the capability adapters.
#[derive(Debug, Clone, Error)]
pub enum McpError {
    /// Transport / framing failure (oversized frame, websocket close,
    /// malformed UTF-8). Never produced by an adapter — only the
    /// transport layer emits this.
    #[error("transport: {0}")]
    Transport(String),

    /// In-band auth denial (e.g. "this token's resource scheme list
    /// doesn't include `corlinman://memory/`"). Pre-upgrade auth is
    /// HTTP 401, not this variant.
    #[error("auth: {0}")]
    Auth(String),

    /// Client sent a non-`initialize` method while the session is in
    /// `Connected` state. JSON-RPC code -32002 (corlinman extension).
    #[error("session not initialized; expected `initialize` first")]
    SessionNotInitialized,

    /// Token's `tools_allowlist` rejects the requested tool name.
    /// JSON-RPC code -32001 (corlinman extension). Tool-call protocol
    /// failure (allowlist denial), not a runtime failure — runtime
    /// failures land in `CallResult { is_error: true }` per MCP
    /// convention, not in this enum.
    #[error("tool not allowed: {0}")]
    ToolNotAllowed(String),

    /// Method string doesn't match any known capability route.
    /// JSON-RPC code -32601.
    #[error("method not found: {0}")]
    MethodNotFound(String),

    /// `params` failed to deserialize, or carried a value the adapter
    /// can't fulfil (unknown resource URI, unknown prompt name, etc.).
    /// JSON-RPC code -32602. Optional `data` payload echoes the
    /// offending value back to the client.
    #[error("invalid params: {message}")]
    InvalidParams {
        message: String,
        data: Option<JsonValue>,
    },

    /// Request envelope malformed (bad `jsonrpc` literal, missing
    /// `method`, etc.). JSON-RPC code -32600.
    #[error("invalid request: {0}")]
    InvalidRequest(String),

    /// Inbound bytes weren't valid JSON. JSON-RPC code -32700.
    #[error("parse error: {0}")]
    ParseError(String),

    /// Catch-all for adapter-internal failures (DB, plugin runtime
    /// panic surfaced as Result, etc.). JSON-RPC code -32603.
    #[error("internal: {0}")]
    Internal(String),
}

impl McpError {
    /// Convenience constructor for `InvalidParams` without a data
    /// payload.
    pub fn invalid_params(message: impl Into<String>) -> Self {
        Self::InvalidParams {
            message: message.into(),
            data: None,
        }
    }

    /// Convenience constructor for `InvalidParams` with a JSON `data`
    /// payload — typically the offending field echoed back.
    pub fn invalid_params_with(message: impl Into<String>, data: JsonValue) -> Self {
        Self::InvalidParams {
            message: message.into(),
            data: Some(data),
        }
    }

    /// JSON-RPC 2.0 code this variant maps to. Public so call sites
    /// that need only the numeric code (logging, metrics) don't have
    /// to construct a full [`JsonRpcError`].
    pub fn jsonrpc_code(&self) -> i32 {
        match self {
            Self::ParseError(_) => error_codes::PARSE_ERROR,
            Self::InvalidRequest(_) => error_codes::INVALID_REQUEST,
            Self::MethodNotFound(_) => error_codes::METHOD_NOT_FOUND,
            Self::InvalidParams { .. } => error_codes::INVALID_PARAMS,
            Self::ToolNotAllowed(_) => error_codes::TOOL_NOT_ALLOWED,
            Self::SessionNotInitialized => error_codes::SESSION_NOT_INITIALIZED,
            Self::Transport(_) | Self::Auth(_) | Self::Internal(_) => error_codes::INTERNAL_ERROR,
        }
    }
}

impl From<McpError> for JsonRpcError {
    fn from(err: McpError) -> Self {
        let code = err.jsonrpc_code();
        match err {
            McpError::InvalidParams { message, data } => {
                let mut e = JsonRpcError::new(code, message);
                if let Some(d) = data {
                    e = e.with_data(d);
                }
                e
            }
            // `Display` impl carries the message for the rest of the
            // variants — that's the canonical wire string.
            other => JsonRpcError::new(code, other.to_string()),
        }
    }
}

impl From<serde_json::Error> for McpError {
    fn from(err: serde_json::Error) -> Self {
        // serde_json failures during request parsing surface as
        // ParseError; failures during params decoding land in
        // InvalidParams. Default mapping is ParseError because that's
        // the boundary where we lift &[u8] → JsonRpcRequest. Callers
        // decoding `params` should construct InvalidParams explicitly.
        Self::ParseError(err.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parse_error_maps_to_negative_32700() {
        let err = McpError::ParseError("unexpected eof".into());
        let rpc: JsonRpcError = err.into();
        assert_eq!(rpc.code, error_codes::PARSE_ERROR);
        assert_eq!(rpc.code, -32700);
        assert!(rpc.message.contains("parse error"));
        assert!(rpc.data.is_none());
    }

    #[test]
    fn invalid_request_maps_to_negative_32600() {
        let rpc: JsonRpcError = McpError::InvalidRequest("missing method".into()).into();
        assert_eq!(rpc.code, error_codes::INVALID_REQUEST);
        assert_eq!(rpc.code, -32600);
    }

    #[test]
    fn method_not_found_maps_to_negative_32601_and_carries_method_name() {
        let rpc: JsonRpcError = McpError::MethodNotFound("tools/bogus".into()).into();
        assert_eq!(rpc.code, error_codes::METHOD_NOT_FOUND);
        assert_eq!(rpc.code, -32601);
        assert!(
            rpc.message.contains("tools/bogus"),
            "wire message must echo the offending method, got {:?}",
            rpc.message
        );
    }

    #[test]
    fn invalid_params_maps_to_negative_32602_without_data() {
        let rpc: JsonRpcError = McpError::invalid_params("unknown resource uri").into();
        assert_eq!(rpc.code, error_codes::INVALID_PARAMS);
        assert_eq!(rpc.code, -32602);
        assert_eq!(rpc.message, "unknown resource uri");
        assert!(rpc.data.is_none());
    }

    #[test]
    fn invalid_params_preserves_data_payload() {
        let rpc: JsonRpcError =
            McpError::invalid_params_with("unknown prompt name", json!({"name": "missing-skill"}))
                .into();
        assert_eq!(rpc.code, error_codes::INVALID_PARAMS);
        assert_eq!(rpc.data, Some(json!({"name": "missing-skill"})));
    }

    #[test]
    fn session_not_initialized_uses_corlinman_extension_code() {
        let err = McpError::SessionNotInitialized;
        assert_eq!(err.jsonrpc_code(), error_codes::SESSION_NOT_INITIALIZED);
        assert_eq!(err.jsonrpc_code(), -32002);
        let rpc: JsonRpcError = err.into();
        assert_eq!(rpc.code, -32002);
        // Spec says servers should keep the message human-readable —
        // the Display impl already does that.
        assert!(
            rpc.message.contains("session not initialized"),
            "got {:?}",
            rpc.message
        );
    }

    #[test]
    fn tool_not_allowed_uses_corlinman_extension_code() {
        let rpc: JsonRpcError = McpError::ToolNotAllowed("kb:search".into()).into();
        assert_eq!(rpc.code, error_codes::TOOL_NOT_ALLOWED);
        assert_eq!(rpc.code, -32001);
        assert!(rpc.message.contains("kb:search"));
    }

    #[test]
    fn transport_auth_internal_all_collapse_to_negative_32603() {
        for err in [
            McpError::Transport("ws closed".into()),
            McpError::Auth("scheme not allowed".into()),
            McpError::Internal("db pool exhausted".into()),
        ] {
            let code = err.jsonrpc_code();
            let rpc: JsonRpcError = err.into();
            assert_eq!(code, error_codes::INTERNAL_ERROR);
            assert_eq!(rpc.code, -32603);
        }
    }

    #[test]
    fn serde_json_error_lifts_into_parse_error_variant() {
        // Trigger a serde_json failure deterministically.
        let raw = b"{\"jsonrpc\":\"2.0\",\"method\":";
        let err = serde_json::from_slice::<crate::schema::JsonRpcRequest>(raw)
            .expect_err("malformed json must fail");
        let mcp: McpError = err.into();
        assert_eq!(mcp.jsonrpc_code(), error_codes::PARSE_ERROR);
        let msg = mcp.to_string();
        assert!(
            msg.contains("parse error"),
            "Display impl must prefix the variant, got {msg:?}",
        );
    }

    #[test]
    fn invalid_params_round_trips_through_jsonrpc_error_to_response_envelope() {
        // Lift through the wire envelope to confirm the data field
        // round-trips when serialized.
        use crate::schema::JsonRpcResponse;
        let rpc: JsonRpcError =
            McpError::invalid_params_with("bad arg", json!({"field": "limit"})).into();
        let resp = JsonRpcResponse::err(json!("req-1"), rpc);
        let wire = serde_json::to_value(&resp).unwrap();
        assert_eq!(wire["error"]["code"], -32602);
        assert_eq!(wire["error"]["message"], "bad arg");
        assert_eq!(wire["error"]["data"], json!({"field": "limit"}));
    }
}
