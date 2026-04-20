//! axum Router construction + HTTP server bootstrap.
//!
//! Later milestones fold the tonic gRPC server (VectorService + PluginBridge)
//! into this same entry point; this first revision only wires axum.

use std::net::SocketAddr;
use std::sync::Arc;

use axum::Router;
use corlinman_agent_client::client::{connect_channel, resolve_endpoint, AgentClient};
use tokio::net::TcpListener;

use crate::routes;
use crate::routes::chat::{grpc::GrpcBackend, ChatBackend, ChatState};

/// Build the top-level axum router with the default (stub) chat route.
///
/// Returns 501 for `/v1/chat/completions` — use [`build_router_with_backend`]
/// to wire the real gRPC backend.
pub fn build_router() -> Router {
    routes::router()
}

/// Build the router with a concrete [`ChatBackend`]. Used both by `main` and
/// by integration tests that want a running handler.
pub fn build_router_with_backend(backend: Arc<dyn ChatBackend>) -> Router {
    let state = ChatState::new(backend);
    routes::router_with_chat_state(state)
}

/// Connect to the Python gRPC agent server; falls back to the stub router
/// when the agent isn't reachable (so `/health` stays up even if Python died).
pub async fn build_router_for_runtime() -> Router {
    let endpoint = resolve_endpoint();
    match connect_channel(&endpoint).await {
        Ok(channel) => {
            tracing::info!(endpoint = %endpoint, "agent client connected");
            let client = AgentClient::new(channel);
            build_router_with_backend(Arc::new(GrpcBackend::new(client)))
        }
        Err(err) => {
            tracing::warn!(
                endpoint = %endpoint,
                error = %err,
                "agent client unreachable; /v1/chat/completions will 501",
            );
            build_router()
        }
    }
}

/// Bind `addr` and serve until `shutdown` resolves.
pub async fn run<F>(addr: SocketAddr, shutdown: F) -> anyhow::Result<()>
where
    F: std::future::Future<Output = ()> + Send + 'static,
{
    let router = build_router_for_runtime().await;
    let listener = TcpListener::bind(addr).await?;
    tracing::info!(%addr, "gateway listening");
    axum::serve(listener, router)
        .with_graceful_shutdown(shutdown)
        .await?;
    Ok(())
}
