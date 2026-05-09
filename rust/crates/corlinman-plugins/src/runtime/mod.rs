//! Plugin runtime abstraction — one implementation per transport.
//!
//! corlinman plugins come in three flavours (see `manifest::PluginType`):
//!   - `sync`    -> `jsonrpc_stdio::execute`, spawn-per-call, returns a result
//!   - `async`   -> `jsonrpc_stdio::execute`, may return `AcceptedForLater(task_id)`
//!   - `service` -> `service_grpc`, long-lived gRPC server (stub today)
//!
//! The trait lives here so later work can wire richer sandboxing or mock
//! runtimes without touching call sites.

use std::sync::Arc;

use async_trait::async_trait;
use bytes::Bytes;
use tokio_util::sync::CancellationToken;

use corlinman_core::CorlinmanError;

pub mod jsonrpc_stdio;
pub mod mcp;
pub mod mcp_stdio;
pub mod service_grpc;

/// Structured input handed to every runtime invocation.
///
/// `args_json` is raw bytes so it can be passed through zero-copy from the
/// gRPC `ToolCall.args_json` field.
#[derive(Debug, Clone)]
pub struct PluginInput {
    pub plugin: String,
    pub tool: String,
    pub args_json: Bytes,
    pub call_id: String,
    pub session_key: String,
    pub trace_id: String,
    /// Working directory for the child process; always the plugin's manifest dir.
    pub cwd: std::path::PathBuf,
    /// Environment variables (already filtered against an allowlist).
    pub env: Vec<(String, String)>,
    /// Deadline hint in milliseconds; runtimes enforce via `tokio::time::timeout`.
    pub deadline_ms: Option<u64>,
}

/// Terminal result from a runtime invocation.
#[derive(Debug, Clone)]
pub enum PluginOutput {
    /// Successful completion; `content` is the JSON-RPC `result` payload.
    Success { content: Bytes, duration_ms: u64 },
    /// Plugin returned a JSON-RPC error object.
    Error {
        code: i64,
        message: String,
        duration_ms: u64,
    },
    /// Async plugin accepted the call; gateway must park and await callback.
    AcceptedForLater { task_id: String, duration_ms: u64 },
}

impl PluginOutput {
    pub fn success(content: Bytes, duration_ms: u64) -> Self {
        Self::Success {
            content,
            duration_ms,
        }
    }

    pub fn error(code: i64, message: impl Into<String>, duration_ms: u64) -> Self {
        Self::Error {
            code,
            message: message.into(),
            duration_ms,
        }
    }
}

/// Core runtime trait. Every runtime must honour `cancel` (cooperative shutdown)
/// and must not block the executor (use `tokio::process::Command`, not `std::process`).
#[async_trait]
pub trait PluginRuntime: Send + Sync + 'static {
    /// Execute one tool invocation.
    async fn execute(
        &self,
        input: PluginInput,
        progress: Option<Arc<dyn ProgressSink>>,
        cancel: CancellationToken,
    ) -> Result<PluginOutput, CorlinmanError>;

    /// Human-readable identifier for logs / metrics (e.g. "jsonrpc_stdio").
    fn kind(&self) -> &'static str;

    /// Which tool-call protocol this runtime prefers when the plugin's
    /// manifest advertises more than one. Returning `None` means "no
    /// preference — use policy order". Default: `Some("openai_function")`.
    ///
    /// The dispatcher in `protocol::dispatcher` consults this only when two
    /// protocols are otherwise equally valid for the same invocation.
    fn preferred_protocol(&self) -> Option<&str> {
        Some("openai_function")
    }
}

/// Streaming progress callback; corresponds 1:1 to `ToolEvent::Progress` in proto.
#[async_trait]
pub trait ProgressSink: Send + Sync {
    async fn emit(&self, message: String, fraction: Option<f32>);
}
