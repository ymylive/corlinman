//! Bridges ServerFrame::ToolCall → corlinman-plugins registry → ClientFrame::ToolResult.
//!
//! In M1/M2 we only **observe** tool calls — no real plugin runtime is wired
//! yet. The [`PlaceholderExecutor`] default returns an `awaiting_plugin_runtime`
//! result so the Python side can continue (or end) the loop without blocking
//! on plugin execution that lands in M3.

use async_trait::async_trait;
use corlinman_core::CorlinmanError;
use corlinman_proto::v1::{ToolCall, ToolResult};
use serde_json::json;

/// Contract every plugin bridge implements so this crate never takes a direct
/// dependency on `corlinman-plugins` (injected at gateway assembly time).
#[async_trait]
pub trait ToolExecutor: Send + Sync {
    /// Execute a tool call and return the result.
    async fn execute(&self, call: &ToolCall) -> Result<ToolResult, CorlinmanError>;
}

/// M1/M2 default: acknowledges the call with a placeholder payload so the
/// Python reasoning loop can advance. M3 replaces this with the real
/// `corlinman-plugins::Registry` behind the same trait.
pub struct PlaceholderExecutor;

#[async_trait]
impl ToolExecutor for PlaceholderExecutor {
    async fn execute(&self, call: &ToolCall) -> Result<ToolResult, CorlinmanError> {
        let payload = json!({
            "status": "awaiting_plugin_runtime",
            "plugin": call.plugin,
            "tool": call.tool,
            "message": "plugin runtime lands in M3; call observed but not executed",
        });
        Ok(ToolResult {
            call_id: call.call_id.clone(),
            result_json: serde_json::to_vec(&payload)?,
            is_error: false,
            duration_ms: 0,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn placeholder_returns_awaiting_payload() {
        let exec = PlaceholderExecutor;
        let call = ToolCall {
            call_id: "c1".into(),
            plugin: "FooPlugin".into(),
            tool: "do_thing".into(),
            args_json: b"{}".to_vec(),
            seq: 0,
        };
        let result = exec.execute(&call).await.unwrap();
        assert_eq!(result.call_id, "c1");
        assert!(!result.is_error);
        let v: serde_json::Value = serde_json::from_slice(&result.result_json).unwrap();
        assert_eq!(v["status"], "awaiting_plugin_runtime");
        assert_eq!(v["plugin"], "FooPlugin");
    }
}
