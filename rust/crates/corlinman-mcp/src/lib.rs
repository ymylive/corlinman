//! corlinman MCP server (Model Context Protocol 2024-11-05).
//!
//! See `docs/design/phase4-w3-c1-design.md` for the full picture.
//!
//! Iter 1 ships only the wire-schema substrate (`schema`); subsequent
//! iters layer error mapping, session state, transport, and the three
//! capability adapters on top. The `schema` module is also reused
//! verbatim by the C2 outbound MCP-stdio plugin client.

pub mod adapters;
pub mod error;
pub mod schema;
pub mod server;

pub use adapters::{
    CapabilityAdapter, PromptsAdapter, ResourcesAdapter, SessionContext, ToolsAdapter,
};
pub use error::McpError;
pub use schema::{
    error_codes, JsonRpcError, JsonRpcRequest, JsonRpcResponse, JSONRPC_VERSION,
    MCP_PROTOCOL_VERSION,
};
pub use server::{
    AdapterDispatcher, McpServer, McpServerConfig, ServerInfo, SessionPhase, SessionState,
    TokenAcl, DEFAULT_TENANT_ID,
};
