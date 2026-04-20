//! corlinman-gateway binary entry point.
//!
//! Boot sequence:
//!   1. install tracing_subscriber (JSON to stdout, `RUST_LOG` respected)
//!   2. resolve listen address (`PORT` env override, default 6005)
//!   3. build the axum router + shared `ChatBackend` handle
//!   4. optionally load `CORLINMAN_CONFIG` and, if `[channels.qq].enabled`,
//!      spawn the QQ channel task bound to the same backend
//!   5. serve axum with graceful-shutdown wired to SIGTERM/SIGINT
//!   6. on signal, cancel child tasks + `std::process::exit(143)`

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use corlinman_core::config::Config;
use corlinman_gateway::routes::chat::ChatBackend;
use corlinman_gateway::services::ChatService as GatewayChatService;
use corlinman_gateway::{server, shutdown};
use corlinman_gateway_api::ChatService as ChatServiceTrait;
use corlinman_plugins::registry::watcher::{HotReloader, DEFAULT_DEBOUNCE};
use corlinman_plugins::runtime::service_grpc::ServiceRuntime;
use corlinman_plugins::{PluginSupervisor, PluginType};
use tokio_util::sync::CancellationToken;
use tracing_subscriber::{fmt, prelude::*, EnvFilter};

/// Default root directory for per-plugin UDS files. Mirrors the env the
/// plugin child sees in `CORLINMAN_PLUGIN_ADDR`.
const DEFAULT_SOCKET_ROOT: &str = "/tmp/corlinman-plugins";

#[tokio::main]
async fn main() {
    init_tracing();

    let addr = resolve_addr();
    tracing::info!(%addr, "starting corlinman-gateway");

    // Root cancellation token. Cancels gRPC/channels/axum on shutdown.
    let root = CancellationToken::new();

    // Build router + keep a handle on the shared backend + registry.
    let (router, backend, plugin_registry) = server::build_runtime().await;

    // Boot the long-lived service-plugin stack: spawn every
    // `plugin_type = "service"` manifest into a supervised child process,
    // dial it over UDS, and start a watchdog per plugin so crashes respawn
    // with backoff. Failures are logged but non-fatal — the gateway keeps
    // serving sync / async plugins + non-plugin routes.
    let service_runtime = Arc::new(ServiceRuntime::new());
    let supervisor = Arc::new(PluginSupervisor::new(std::path::PathBuf::from(
        DEFAULT_SOCKET_ROOT,
    )));
    for entry in plugin_registry.list() {
        if entry.manifest.plugin_type != PluginType::Service {
            continue;
        }
        let manifest = entry.manifest.as_ref().clone();
        match supervisor.spawn_service(&manifest).await {
            Ok(socket) => {
                if let Err(err) = service_runtime.register(&manifest.name, &socket).await {
                    tracing::error!(
                        plugin = %manifest.name,
                        error = %err,
                        "service plugin register failed; skipping watchdog",
                    );
                    continue;
                }
                Arc::clone(&supervisor).start_watchdog(
                    manifest.name.clone(),
                    manifest,
                    Arc::clone(&service_runtime),
                );
            }
            Err(err) => tracing::error!(
                plugin = %manifest.name,
                error = %err,
                "service plugin spawn failed at boot",
            ),
        }
    }

    // Spawn the plugin hot reloader. It watches the registry's search roots
    // with `notify` (or falls back to polling) and `upsert`s / `remove`s
    // entries as `plugin-manifest.toml` files change on disk. Cancellation
    // flows from the root shutdown token so the watcher thread drains on
    // SIGTERM alongside the HTTP server.
    let hot_reloader_handle = {
        let roots: Vec<std::path::PathBuf> = plugin_registry
            .roots()
            .iter()
            .map(|r| r.path.clone())
            .collect();
        if roots.is_empty() {
            tracing::debug!("no plugin roots configured; hot reloader not spawned");
            None
        } else {
            let reloader = HotReloader::new(plugin_registry.clone(), roots, DEFAULT_DEBOUNCE);
            let cancel = root.child_token();
            Some(tokio::spawn(async move {
                if let Err(err) = reloader.run(cancel).await {
                    tracing::warn!(error = %err, "plugin hot reloader exited with error");
                }
            }))
        }
    };
    // Optionally launch channel adapters.
    let mut channel_handles: Vec<tokio::task::JoinHandle<()>> = Vec::new();
    if let Some(backend) = backend.as_ref() {
        match load_config() {
            Ok(Some(cfg)) => {
                if let Some(handle) = maybe_spawn_qq_channel(&cfg, backend.clone(), root.clone()) {
                    channel_handles.push(handle);
                }
                if let Some(handle) =
                    maybe_spawn_telegram_channel(&cfg, backend.clone(), root.clone())
                {
                    channel_handles.push(handle);
                }
            }
            Ok(None) => {
                tracing::debug!("no CORLINMAN_CONFIG / config.toml found; channels disabled");
            }
            Err(err) => {
                tracing::warn!(error = %err, "config load failed; channels disabled");
            }
        }
    }

    let server_cancel = root.clone();
    let server_handle = tokio::spawn(async move {
        let shutdown_fut = {
            let token = server_cancel.clone();
            async move { token.cancelled().await }
        };
        if let Err(err) = server::run_with_router(addr, router, shutdown_fut).await {
            tracing::error!(error = %err, "gateway server crashed");
        }
    });

    let reason = shutdown::wait_for_signal().await;
    tracing::info!(?reason, "shutdown signal received, draining");
    root.cancel();

    if let Err(err) = server_handle.await {
        tracing::warn!(error = %err, "server task join failed");
    }
    for h in channel_handles {
        let _ = h.await;
    }
    if let Some(h) = hot_reloader_handle {
        let _ = h.await;
    }

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

/// Load config from `CORLINMAN_CONFIG` if set; otherwise return `Ok(None)` so
/// the gateway can run without a config file (e.g. dev / tests).
fn load_config() -> anyhow::Result<Option<Config>> {
    let Some(path) = std::env::var("CORLINMAN_CONFIG").ok().map(PathBuf::from) else {
        return Ok(None);
    };
    if !path.exists() {
        return Ok(None);
    }
    let cfg = Config::load_from_path(&path)?;
    Ok(Some(cfg))
}

/// If `[channels.qq].enabled` is true, spawn the channel loop and return its
/// join handle. Otherwise returns `None`.
fn maybe_spawn_qq_channel(
    cfg: &Config,
    backend: Arc<dyn ChatBackend>,
    root: CancellationToken,
) -> Option<tokio::task::JoinHandle<()>> {
    let qq_cfg = cfg.channels.qq.as_ref()?;
    if !qq_cfg.enabled {
        return None;
    }
    let model = cfg.models.default.clone();
    let svc: Arc<dyn ChatServiceTrait> = Arc::new(GatewayChatService::new(backend));
    let params = corlinman_channels::service::QqChannelParams {
        config: qq_cfg.clone(),
        model,
        chat_service: svc,
        rate_limit_hook: None,
    };
    let cancel = root.child_token();
    Some(tokio::spawn(async move {
        if let Err(err) = corlinman_channels::service::run_qq_channel(params, cancel).await {
            tracing::error!(error = %err, "qq channel task exited with error");
        }
    }))
}

/// If `[channels.telegram].enabled` is true, spawn the TG long-poll loop.
/// Otherwise returns `None`.
fn maybe_spawn_telegram_channel(
    cfg: &Config,
    backend: Arc<dyn ChatBackend>,
    root: CancellationToken,
) -> Option<tokio::task::JoinHandle<()>> {
    let tg_cfg = cfg.channels.telegram.as_ref()?;
    if !tg_cfg.enabled {
        return None;
    }
    let model = cfg.models.default.clone();
    let svc: Arc<dyn ChatServiceTrait> = Arc::new(GatewayChatService::new(backend));
    let params = corlinman_channels::telegram::TelegramParams {
        config: tg_cfg.clone(),
        chat_service: svc,
        model,
    };
    let cancel = root.child_token();
    Some(tokio::spawn(async move {
        if let Err(err) = corlinman_channels::telegram::run_telegram_channel(params, cancel).await {
            tracing::error!(error = %err, "telegram channel task exited with error");
        }
    }))
}
