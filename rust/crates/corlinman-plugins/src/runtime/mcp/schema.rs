//! MCP wire schema (subset) — duplicated here from `corlinman_mcp::schema`
//! because `corlinman-mcp` already depends on `corlinman-plugins`,
//! making a backward dep a cycle.
//!
//! Why not move the schema to a shared crate today: corlinman-mcp is
//! read-only for the C2 task and the schema sits inside its lib root.
//! The remediation (a `corlinman-mcp-schema` leaf crate that both
//! `corlinman-mcp` and `corlinman-plugins` consume) is the right
//! long-term fix — tracked as a Wave 4 cleanup. Until then this
//! module is the single source of truth for MCP wire types used by
//! the plugin adapter.
//!
//! **Drift policy**: any change to `corlinman_mcp::schema` that the
//! adapter consumes MUST be mirrored here in the same commit. The
//! tests in the adapter (handshake / tools roundtrip) deserialise
//! upstream-shaped JSON, so a divergence shows up as parse failure.
//!
//! Reference: <https://spec.modelcontextprotocol.io/specification/2024-11-05/>

use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;

/// JSON-RPC 2.0 protocol literal.
pub const JSONRPC_VERSION: &str = "2.0";

/// MCP protocol version we speak.
pub const MCP_PROTOCOL_VERSION: &str = "2024-11-05";

fn jsonrpc_default() -> String {
    JSONRPC_VERSION.to_string()
}

fn deserialize_jsonrpc<'de, D>(d: D) -> Result<String, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let s = String::deserialize(d)?;
    if s != JSONRPC_VERSION {
        return Err(serde::de::Error::custom(format!(
            "expected jsonrpc=\"{JSONRPC_VERSION}\", got {s:?}"
        )));
    }
    Ok(s)
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JsonRpcRequest {
    #[serde(default = "jsonrpc_default", deserialize_with = "deserialize_jsonrpc")]
    pub jsonrpc: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<JsonValue>,
    pub method: String,
    #[serde(default)]
    pub params: JsonValue,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum JsonRpcResponse {
    Result {
        #[serde(default = "jsonrpc_default", deserialize_with = "deserialize_jsonrpc")]
        jsonrpc: String,
        id: JsonValue,
        result: JsonValue,
    },
    Error {
        #[serde(default = "jsonrpc_default", deserialize_with = "deserialize_jsonrpc")]
        jsonrpc: String,
        id: JsonValue,
        error: JsonRpcError,
    },
}

impl JsonRpcResponse {
    pub fn err(id: JsonValue, error: JsonRpcError) -> Self {
        Self::Error {
            jsonrpc: JSONRPC_VERSION.into(),
            id,
            error,
        }
    }

    pub fn id(&self) -> &JsonValue {
        match self {
            Self::Result { id, .. } | Self::Error { id, .. } => id,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JsonRpcError {
    pub code: i32,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data: Option<JsonValue>,
}

impl JsonRpcError {
    pub fn new(code: i32, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            data: None,
        }
    }
}

pub mod error_codes {
    pub const PARSE_ERROR: i32 = -32700;
    pub const INVALID_REQUEST: i32 = -32600;
    pub const METHOD_NOT_FOUND: i32 = -32601;
    pub const INVALID_PARAMS: i32 = -32602;
    pub const INTERNAL_ERROR: i32 = -32603;
}

// ---------------- initialize ----------------

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct InitializeParams {
    #[serde(rename = "protocolVersion")]
    pub protocol_version: String,
    #[serde(default)]
    pub capabilities: ClientCapabilities,
    #[serde(rename = "clientInfo")]
    pub client_info: Implementation,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ClientCapabilities {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sampling: Option<JsonValue>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub roots: Option<JsonValue>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub experimental: Option<JsonValue>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Implementation {
    pub name: String,
    pub version: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct InitializeResult {
    #[serde(rename = "protocolVersion")]
    pub protocol_version: String,
    pub capabilities: ServerCapabilities,
    #[serde(rename = "serverInfo")]
    pub server_info: Implementation,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ServerCapabilities {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tools: Option<ToolsCapability>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub resources: Option<JsonValue>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub prompts: Option<JsonValue>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ToolsCapability {
    #[serde(default, rename = "listChanged", skip_serializing_if = "Option::is_none")]
    pub list_changed: Option<bool>,
}

// ---------------- tools/* ----------------

pub mod tools {
    use super::JsonValue;
    use serde::{Deserialize, Serialize};

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    pub struct ListResult {
        pub tools: Vec<ToolDescriptor>,
        #[serde(default, rename = "nextCursor", skip_serializing_if = "Option::is_none")]
        pub next_cursor: Option<String>,
    }

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    pub struct ToolDescriptor {
        pub name: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        pub description: Option<String>,
        #[serde(rename = "inputSchema")]
        pub input_schema: JsonValue,
    }

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    pub struct CallParams {
        pub name: String,
        #[serde(default)]
        pub arguments: JsonValue,
    }

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    pub struct CallResult {
        pub content: Vec<Content>,
        #[serde(default, rename = "isError")]
        pub is_error: bool,
    }

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    #[serde(tag = "type", rename_all = "lowercase")]
    pub enum Content {
        Text {
            text: String,
        },
        Image {
            data: String,
            #[serde(rename = "mimeType")]
            mime_type: String,
        },
    }
}
