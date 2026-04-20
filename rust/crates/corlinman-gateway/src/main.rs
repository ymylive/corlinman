//! corlinman-gateway binary entry point.
//!
//! Boot sequence (current milestone — later ones add config / AppState / gRPC):
//!   1. install tracing_subscriber (JSON to stdout, `RUST_LOG` respected)
//!   2. resolve listen address (`PORT` env override, default 6005)
//!   3. serve axum router with graceful-shutdown wired to SIGTERM/SIGINT
//!   4. on signal, drain + `std::process::exit(143)` (openclaw convention)

use std::net::SocketAddr;

use corlinman_gateway::{server, shutdown};
use tokio_util::sync::CancellationToken;
use tracing_subscriber::{fmt, prelude::*, EnvFilter};

#[tokio::main]
async fn main() {
    init_tracing();

    let addr = resolve_addr();
    tracing::info!(%addr, "starting corlinman-gateway");

    // Root cancellation token. In later milestones this also gates the gRPC
    // server, channel adapters, and the scheduler; for now it only signals
    // the axum graceful shutdown.
    let root = CancellationToken::new();
    let server_cancel = root.clone();

    let server_handle = tokio::spawn(async move {
        let shutdown_fut = {
            let token = server_cancel.clone();
            async move { token.cancelled().await }
        };
        if let Err(err) = server::run(addr, shutdown_fut).await {
            tracing::error!(error = %err, "gateway server crashed");
        }
    });

    let reason = shutdown::wait_for_signal().await;
    tracing::info!(?reason, "shutdown signal received, draining");
    root.cancel();

    if let Err(err) = server_handle.await {
        tracing::warn!(error = %err, "server task join failed");
    }

    // Flush tracing is implicit for stdout; later milestones will call
    // `tracing_appender::non_blocking::WorkerGuard::drop` on a rolling file.
    std::process::exit(shutdown::EXIT_CODE_ON_SIGNAL);
}

fn init_tracing() {
    let env_filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    tracing_subscriber::registry()
        .with(env_filter)
        .with(fmt::layer().json().with_current_span(false))
        .init();
}

fn resolve_addr() -> SocketAddr {
    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(6005);
    SocketAddr::from(([127, 0, 0, 1], port))
}
