//! JSON-RPC 2.0 envelope and MCP 2024-11-05 capability payloads.
//!
//! Wire types only — no transport, no async, no axum. The C2 outbound
//! MCP-stdio plugin adapter reuses this module verbatim, which is why
//! it sits at the top of the crate and depends only on `serde` /
//! `serde_json`.
//!
//! Reference: <https://spec.modelcontextprotocol.io/specification/2024-11-05/>

use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;

/// JSON-RPC 2.0 protocol version literal. Required on every frame.
pub const JSONRPC_VERSION: &str = "2.0";

/// MCP protocol version we implement.
pub const MCP_PROTOCOL_VERSION: &str = "2024-11-05";

/// Helper for the `jsonrpc` literal field — serializes as `"2.0"` and
/// rejects any other value on deserialize.
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

/// JSON-RPC request frame.
///
/// Per spec, `id` is `string | number | null`. A *missing* id makes
/// the frame a notification (no response expected). We model that as
/// `Option<JsonValue>`; round-trip preserves the distinction.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JsonRpcRequest {
    #[serde(
        default = "jsonrpc_default",
        deserialize_with = "deserialize_jsonrpc"
    )]
    pub jsonrpc: String,
    /// Notification when `None`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<JsonValue>,
    pub method: String,
    /// JSON-RPC spec allows `params` to be omitted entirely; we
    /// default it to `Null`.
    #[serde(default)]
    pub params: JsonValue,
}

impl JsonRpcRequest {
    /// True when `id` is absent — i.e. the frame is a notification
    /// per JSON-RPC 2.0 §4.
    pub fn is_notification(&self) -> bool {
        self.id.is_none()
    }
}

/// JSON-RPC response frame. Either `result` or `error`, never both.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum JsonRpcResponse {
    Result {
        #[serde(
            default = "jsonrpc_default",
            deserialize_with = "deserialize_jsonrpc"
        )]
        jsonrpc: String,
        id: JsonValue,
        result: JsonValue,
    },
    Error {
        #[serde(
            default = "jsonrpc_default",
            deserialize_with = "deserialize_jsonrpc"
        )]
        jsonrpc: String,
        id: JsonValue,
        error: JsonRpcError,
    },
}

impl JsonRpcResponse {
    pub fn ok(id: JsonValue, result: JsonValue) -> Self {
        Self::Result {
            jsonrpc: JSONRPC_VERSION.into(),
            id,
            result,
        }
    }

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

/// JSON-RPC 2.0 error object.
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

    pub fn with_data(mut self, data: JsonValue) -> Self {
        self.data = Some(data);
        self
    }
}

/// Standard JSON-RPC 2.0 error codes (§5.1) plus MCP / corlinman
/// extensions in the implementation-defined range (-32000..=-32099).
pub mod error_codes {
    /// JSON-RPC 2.0 §5.1
    pub const PARSE_ERROR: i32 = -32700;
    pub const INVALID_REQUEST: i32 = -32600;
    pub const METHOD_NOT_FOUND: i32 = -32601;
    pub const INVALID_PARAMS: i32 = -32602;
    pub const INTERNAL_ERROR: i32 = -32603;

    /// corlinman MCP extensions.
    pub const TOOL_NOT_ALLOWED: i32 = -32001;
    pub const SESSION_NOT_INITIALIZED: i32 = -32002;
}

// ---------------------------------------------------------------------
// `initialize` handshake payloads
// ---------------------------------------------------------------------

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
    pub resources: Option<ResourcesCapability>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub prompts: Option<PromptsCapability>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ToolsCapability {
    #[serde(default, rename = "listChanged", skip_serializing_if = "Option::is_none")]
    pub list_changed: Option<bool>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ResourcesCapability {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub subscribe: Option<bool>,
    #[serde(default, rename = "listChanged", skip_serializing_if = "Option::is_none")]
    pub list_changed: Option<bool>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct PromptsCapability {
    #[serde(default, rename = "listChanged", skip_serializing_if = "Option::is_none")]
    pub list_changed: Option<bool>,
}

// ---------------------------------------------------------------------
// tools/* payloads
// ---------------------------------------------------------------------

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

    /// MCP `Content` block. C1 emits only `text`; richer variants
    /// land with the adapters in iter 5+.
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

    impl Content {
        pub fn text(t: impl Into<String>) -> Self {
            Self::Text { text: t.into() }
        }
    }
}

// ---------------------------------------------------------------------
// resources/* payloads
// ---------------------------------------------------------------------

pub mod resources {
    use super::JsonValue;
    use serde::{Deserialize, Serialize};

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    pub struct ListParams {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        pub cursor: Option<String>,
    }

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    pub struct ListResult {
        pub resources: Vec<Resource>,
        #[serde(default, rename = "nextCursor", skip_serializing_if = "Option::is_none")]
        pub next_cursor: Option<String>,
    }

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    pub struct Resource {
        pub uri: String,
        pub name: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        pub description: Option<String>,
        #[serde(default, rename = "mimeType", skip_serializing_if = "Option::is_none")]
        pub mime_type: Option<String>,
    }

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    pub struct ReadParams {
        pub uri: String,
    }

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    pub struct ReadResult {
        pub contents: Vec<ResourceContent>,
    }

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    #[serde(untagged)]
    pub enum ResourceContent {
        Text {
            uri: String,
            #[serde(default, rename = "mimeType", skip_serializing_if = "Option::is_none")]
            mime_type: Option<String>,
            text: String,
        },
        Blob {
            uri: String,
            #[serde(default, rename = "mimeType", skip_serializing_if = "Option::is_none")]
            mime_type: Option<String>,
            blob: String,
        },
    }

    impl ResourceContent {
        pub fn text(uri: impl Into<String>, text: impl Into<String>) -> Self {
            Self::Text {
                uri: uri.into(),
                mime_type: None,
                text: text.into(),
            }
        }
    }

    /// Marker — silences "unused import" warnings if a downstream
    /// file pulls only `JsonValue`.
    #[allow(dead_code)]
    pub(crate) fn _touch(_: JsonValue) {}
}

// ---------------------------------------------------------------------
// prompts/* payloads
// ---------------------------------------------------------------------

pub mod prompts {
    use super::JsonValue;
    use serde::{Deserialize, Serialize};

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    pub struct ListResult {
        pub prompts: Vec<Prompt>,
        #[serde(default, rename = "nextCursor", skip_serializing_if = "Option::is_none")]
        pub next_cursor: Option<String>,
    }

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    pub struct Prompt {
        pub name: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        pub description: Option<String>,
        #[serde(default, skip_serializing_if = "Vec::is_empty")]
        pub arguments: Vec<PromptArgument>,
    }

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    pub struct PromptArgument {
        pub name: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        pub description: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        pub required: Option<bool>,
    }

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    pub struct GetParams {
        pub name: String,
        #[serde(default)]
        pub arguments: JsonValue,
    }

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    pub struct GetResult {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        pub description: Option<String>,
        pub messages: Vec<PromptMessage>,
    }

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    pub struct PromptMessage {
        pub role: PromptRole,
        pub content: PromptContent,
    }

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    #[serde(rename_all = "lowercase")]
    pub enum PromptRole {
        User,
        Assistant,
        System,
    }

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    #[serde(tag = "type", rename_all = "lowercase")]
    pub enum PromptContent {
        Text { text: String },
    }

    impl PromptContent {
        pub fn text(t: impl Into<String>) -> Self {
            Self::Text { text: t.into() }
        }
    }
}
