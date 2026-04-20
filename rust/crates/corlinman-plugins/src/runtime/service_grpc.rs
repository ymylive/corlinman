//! Long-lived gRPC service runtime (stub for M3).
//!
//! Service plugins run continuously: the gateway spawns the plugin once on
//! boot, hands it a UDS path via the `CORLINMAN_PLUGIN_ADDR` environment
//! variable, and then talks to it over gRPC for every tool call.
//!
//! The trait + types live here so `PluginRegistry` can reference them without
//! the actual server implementation. Real boot / dispatch code lands in a
//! later milestone; for now `execute` simply returns an "unimplemented"
//! runtime error so callers fail loudly.

use std::sync::Arc;

use async_trait::async_trait;
use tokio_util::sync::CancellationToken;

use corlinman_core::CorlinmanError;

use crate::runtime::{PluginInput, PluginOutput, PluginRuntime, ProgressSink};

/// Environment variable the gateway exports so the plugin knows where to
/// bind its gRPC server (UDS path on Unix).
pub const PLUGIN_ADDR_ENV: &str = "CORLINMAN_PLUGIN_ADDR";

/// Stub implementation — returns a clear runtime error until the boot /
/// dispatch code is wired in a later milestone.
#[derive(Debug, Clone, Default)]
pub struct ServiceGrpcRuntime;

#[async_trait]
impl PluginRuntime for ServiceGrpcRuntime {
    async fn execute(
        &self,
        input: PluginInput,
        _progress: Option<Arc<dyn ProgressSink>>,
        _cancel: CancellationToken,
    ) -> Result<PluginOutput, CorlinmanError> {
        // TODO(M3-late): spawn plugin with CORLINMAN_PLUGIN_ADDR, connect via
        // tonic, and forward the ToolCall.
        Err(CorlinmanError::PluginRuntime {
            plugin: input.plugin,
            message: "service gRPC runtime is not implemented yet".into(),
        })
    }

    fn kind(&self) -> &'static str {
        "service_grpc"
    }
}
