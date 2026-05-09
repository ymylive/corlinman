//! MCP runtime sub-modules.
//!
//! Iter 4 layers:
//!   - `redact`  (env-passthrough filtering + log redaction; iter 3)
//!   - `client`  (line-delimited JSON-RPC stdio client with response demux)
//!   - `adapter` (spawn → initialize handshake → state machine)
//!
//! Iter 5 will add `tools/list` + `tools/call` to `adapter`; iter 6
//! introduces a supervisor module that wraps `adapter::start_one`
//! with crash-restart + backoff.

pub mod adapter;
pub mod client;
pub mod redact;
pub mod schema;

pub use adapter::{AdapterError, AdapterStatus, McpAdapter};
pub use client::{ClientError as McpClientError, McpStdioClient};
