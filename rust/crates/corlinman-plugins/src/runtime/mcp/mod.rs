//! MCP runtime sub-modules.
//!
//!   - `redact`     (env-passthrough filtering + log redaction; iter 3)
//!   - `client`     (line-delimited JSON-RPC stdio client; iter 4)
//!   - `adapter`    (spawn → initialize → tools/list + tools/call; iters 4-5)
//!   - `supervisor` (crash-restart watcher; iter 6)
//!   - `dispatch`   (PluginRuntime trait impl; iter 7)
//!   - `schema`     (vendored MCP wire types; see schema.rs preamble)

pub mod adapter;
pub mod client;
pub mod dispatch;
pub mod redact;
pub mod schema;
pub mod supervisor;

pub use adapter::{AdapterError, AdapterStatus, McpAdapter};
pub use client::{ClientError as McpClientError, McpStdioClient};
pub use dispatch::McpRuntime;
pub use supervisor::{
    default_backoff, spawn_supervisor, SupervisorHandle, SupervisorPolicy, SupervisorStats,
};
