//! Outbound MCP client peer.
//!
//! C1 ships **only** the wire-level skeleton needed to unblock C2's
//! "kind = mcp" plugin adapter. Iter 4 lands [`stdio::connect_stdio`]:
//! spawn a child process, frame line-delimited JSON-RPC over its
//! stdin/stdout, and dispatch responses back to callers keyed by
//! request id.
//!
//! C2 will layer richer features on top (timeouts, capability
//! negotiation, reconnect, signal handling) — none of which belong in
//! the corlinman-mcp crate. This module is intentionally minimal:
//!
//!   - one async sender ([`McpClient::call`]) per JSON-RPC request,
//!   - oneshot-based response demux (reuses the wstool pattern),
//!   - graceful shutdown on `Drop`.
//!
//! No transport-level retries, no schema introspection, no stdio
//! transport variants beyond plain newline-delimited JSON. That's all
//! C2's job.

pub mod stdio;

pub use stdio::{McpClient, McpClientError};
