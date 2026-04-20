//! corlinman-agent-client — bidirectional gRPC client to the Python agent.
//!
//! Responsibilities split across submodules:
//!   - `client` / `stream` — connection + backpressured bidi
//!   - `classify` — upstream error → `FailoverReason`
//!   - `retry` — backoff orchestration (uses `corlinman_core::backoff`)
//!   - `tool_callback` — glue between ToolCall → PluginBridge → ToolResult

pub mod classify;
pub mod client;
pub mod retry;
pub mod stream;
pub mod tool_callback;
