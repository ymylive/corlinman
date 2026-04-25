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

use corlinman_core::config::{Config, FileLoggingConfig};
use corlinman_evolution::{EvolutionStore, SignalsRepo};
use corlinman_gateway::config_watcher::ConfigWatcher;
use corlinman_gateway::evolution_observer;
use corlinman_gateway::grpc::{
    serve_placeholder, PlaceholderService, DEFAULT_RUST_SOCKET, ENV_RUST_SOCKET,
};
use corlinman_gateway::log_broadcast::{
    BroadcastLayer, BroadcastLayerSpans, LogRecord, DEFAULT_CAPACITY,
};
use corlinman_gateway::log_retention;
use corlinman_gateway::services::ChatService as GatewayChatService;
use corlinman_gateway::telemetry::FileSink;
use corlinman_gateway::{server, shutdown, telemetry};
use corlinman_gateway_api::ChatService as ChatServiceTrait;
use corlinman_plugins::registry::watcher::{HotReloader, DEFAULT_DEBOUNCE};
use corlinman_plugins::runtime::service_grpc::ServiceRuntime;
use corlinman_plugins::{PluginSupervisor, PluginType};
use tokio::sync::broadcast;
use tokio_util::sync::CancellationToken;
use tracing_appender::non_blocking::WorkerGuard;
use tracing_subscriber::{fmt, prelude::*, EnvFilter};

/// Default root directory for per-plugin UDS files. Mirrors the env the
/// plugin child sees in `CORLINMAN_PLUGIN_ADDR`.
const DEFAULT_SOCKET_ROOT: &str = "/tmp/corlinman-plugins";

#[tokio::main]
async fn main() {
    // P0-1: pick up the file-logging sub-section early so the appender
    // is wired into the very first `init_tracing()` call. Load is
    // best-effort — a missing / malformed config falls back to stdout
    // only, matching the pre-P0-1 behaviour.
    let file_log_cfg = preload_file_logging_config();

    let (log_tx, _file_guard, retention_spec) = init_tracing(file_log_cfg.as_ref());

    let addr = resolve_addr();
    tracing::info!(%addr, "starting corlinman-gateway");

    // Cross-cutting event bus (B1-BE1). Subscribers pick a priority tier
    // and observe domain events (message received/sent, session patches,
    // config changes, ...). Capacity sized for burst from the QQ channel.
    let hook_bus = corlinman_hooks::HookBus::new(1024);
    if let Err(err) = hook_bus
        .emit(corlinman_hooks::HookEvent::GatewayStartup {
            version: env!("CARGO_PKG_VERSION").into(),
        })
        .await
    {
        tracing::warn!(error = %err, "hook bus startup emit failed");
    }

    // Root cancellation token. Cancels gRPC/channels/axum on shutdown.
    let root = CancellationToken::new();

    // Phase 2 wave 1-A: stand up the EvolutionObserver. Subscribes to the
    // hook bus, adapts the curated event set into `EvolutionSignal`s, and
    // persists them via `corlinman-evolution`'s `SignalsRepo`. Gated by
    // `[evolution.observer.enabled]` (default true). Failures here only
    // warn — gateway startup never blocks on the observer.
    let evolution_observer_handle = maybe_spawn_evolution_observer(&hook_bus).await;

    // P0-1: spawn the log-retention sweeper if a file sink is active.
    // Delete rotated files older than `retention_days` from the log
    // directory every `SWEEP_INTERVAL`. Failure is warn-only.
    let log_retention_handle = retention_spec
        .map(|(dir, prefix, days)| log_retention::spawn(dir, prefix, days, root.child_token()));

    // B5-BE3: build the live `ConfigWatcher` *before* the router so the
    // admin state + canvas state can share its `Arc<ArcSwap<Config>>`. The
    // watcher also owns the in-memory config snapshot every subsystem
    // reads from; a SIGHUP or fs-level edit will swap it in-place.
    let config_watcher = spawn_config_watcher(&hook_bus, root.child_token());

    // Build router + keep a handle on the shared backend + registry.
    // The log broadcast sender threads through so `/admin/logs/stream`
    // can subscribe fresh receivers per request.
    let (router, backend, plugin_registry, _cfg_handle, _cfg_path) = server::build_runtime_full(
        Some(log_tx),
        Some(Arc::new(hook_bus.clone())),
        config_watcher.as_ref().map(|(w, _)| w.clone()),
    )
    .await;

    // B4-BE1: when `[telegram.webhook].public_url` is set, mount the
    // `POST /channels/telegram/webhook` route onto the gateway router.
    // Empty URL = long-poll fallback; the route is not mounted so 404s
    // for stray requests surface loudly. Missing `bot_token` is also a
    // skip so boot stays resilient on fresh installs.
    let router = maybe_mount_telegram_webhook(router, hook_bus.clone()).await;

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
    // Optionally launch channel adapters via the shared `Channel` trait
    // registry (B4-BE2). Each built-in adapter (`qq`, `telegram`) declares
    // its own `enabled()` check against the config; `spawn_all` skips any
    // that return false so the behaviour matches the previous ad-hoc
    // `maybe_spawn_*` pair. External channels can be registered by pushing
    // into a custom `ChannelRegistry` before the call.
    let mut channel_handles: Vec<tokio::task::JoinHandle<anyhow::Result<()>>> = Vec::new();
    if let Some(backend) = backend.as_ref() {
        match load_config() {
            Ok(Some(cfg)) => {
                let svc: Arc<dyn ChatServiceTrait> =
                    Arc::new(GatewayChatService::new(backend.clone()));
                let ctx = corlinman_channels::ChannelContext {
                    config: Arc::new(cfg.clone()),
                    chat_service: svc,
                    model: cfg.models.default.clone(),
                    rate_limit_hook: None,
                    hook_bus: Some(Arc::new(hook_bus.clone())),
                };
                let registry = corlinman_channels::ChannelRegistry::builtin();
                channel_handles = corlinman_channels::spawn_all(&registry, ctx, root.clone());
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

    // B1-BE3: stand up the Rust→Python reverse gRPC surface used by the
    // Python `context_assembler` for placeholder expansion. Bind is best-
    // effort — failure logs and the rest of the gateway keeps serving, so
    // existing HTTP traffic is never blocked on this experimental channel.
    let placeholder_socket =
        std::env::var(ENV_RUST_SOCKET).unwrap_or_else(|_| DEFAULT_RUST_SOCKET.to_string());
    let placeholder_cancel = root.clone();
    let placeholder_handle = tokio::spawn(async move {
        let svc = PlaceholderService::with_empty_engine();
        let shutdown_fut = {
            let token = placeholder_cancel.clone();
            async move { token.cancelled().await }
        };
        if let Err(err) = serve_placeholder(&placeholder_socket, svc, shutdown_fut).await {
            tracing::warn!(error = %err, socket = %placeholder_socket, "placeholder grpc server exited");
        }
    });

    let reason = shutdown::wait_for_signal().await;
    tracing::info!(?reason, "shutdown signal received, draining");
    root.cancel();

    if let Err(err) = server_handle.await {
        tracing::warn!(error = %err, "server task join failed");
    }
    if let Err(err) = placeholder_handle.await {
        tracing::warn!(error = %err, "placeholder grpc task join failed");
    }
    for h in channel_handles {
        match h.await {
            Ok(Ok(())) => {}
            Ok(Err(err)) => tracing::error!(error = %err, "channel task exited with error"),
            Err(join_err) => tracing::warn!(error = %join_err, "channel task join failed"),
        }
    }
    if let Some(h) = hot_reloader_handle {
        let _ = h.await;
    }
    if let Some((_watcher, handle)) = config_watcher {
        let _ = handle.await;
    }
    if let Some(h) = log_retention_handle {
        let _ = h.await;
    }
    if let Some(h) = evolution_observer_handle {
        // The observer's writer task exits cleanly once every sender on
        // its bounded queue is dropped, which happens when the subscriber
        // loop sees the `HookBus` close. We give it a moment to drain;
        // dropping the runtime would otherwise abort it mid-write.
        let _ = h.await;
    }

    // S7.T1: flush + shutdown the OTLP exporter if it was installed. No-op
    // when telemetry was never initialised.
    telemetry::shutdown();

    // Drop the file-appender guard last so any pending writes queued by
    // the shutdown path above make it to disk.
    drop(_file_guard);

    std::process::exit(shutdown::EXIT_CODE_ON_SIGNAL);
}

/// `(dir, prefix, retention_days)` — everything the retention sweeper
/// needs to find and age rotated files. Returned alongside the worker
/// guard so `main` doesn't have to re-read `[logging.file]`.
type RetentionSpec = (PathBuf, String, u32);

/// Return tuple for [`init_tracing`]:
///   * broadcast sender feeding `/admin/logs/stream`;
///   * `WorkerGuard` that must live for the process lifetime (dropping
///     it stops the non-blocking file writer);
///   * optional retention spec — `Some` iff a file sink was initialised.
type TracingInit = (
    broadcast::Sender<LogRecord>,
    Option<WorkerGuard>,
    Option<RetentionSpec>,
);

/// Wire up tracing:
///   - `EnvFilter` from `RUST_LOG` (fallback `info`).
///   - `fmt` layer → JSON to stdout.
///   - `BroadcastLayer` + `BroadcastLayerSpans` → feed `/admin/logs/stream`.
///   - `tracing-opentelemetry` layer when `OTEL_EXPORTER_OTLP_ENDPOINT`
///     is set (S7.T1). Missing / unreachable collector is warn-and-continue.
///   - P0-1: when `file_cfg` is `Some` and its `path` is non-empty, also
///     write JSON events to a rolling file via `tracing-appender`.
fn init_tracing(file_cfg: Option<&FileLoggingConfig>) -> TracingInit {
    let env_filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    let (broadcast_layer, log_tx) = BroadcastLayer::new(DEFAULT_CAPACITY);
    let otel_layer =
        telemetry::try_init_tracer().map(tracing_opentelemetry::OpenTelemetryLayer::new);

    let file_sink: Option<FileSink> = file_cfg.and_then(telemetry::build_file_layer);
    let retention_spec = file_sink
        .as_ref()
        .zip(file_cfg)
        .map(|(sink, cfg)| (sink.dir.clone(), sink.prefix.clone(), cfg.retention_days));
    let file_layer = file_sink.as_ref().map(|sink| {
        fmt::layer()
            .json()
            .with_current_span(false)
            .with_writer(sink.writer.clone())
    });
    let guard = file_sink.map(|s| s.guard);

    tracing_subscriber::registry()
        .with(env_filter)
        .with(fmt::layer().json().with_current_span(false))
        .with(BroadcastLayerSpans)
        .with(broadcast_layer)
        .with(otel_layer)
        .with(file_layer)
        .init();
    (log_tx, guard, retention_spec)
}

/// Best-effort read of `[logging.file]` from the config file resolved
/// via `CORLINMAN_CONFIG` (or the default path). Returns `None` when the
/// file is missing or cannot be parsed — the gateway then keeps the
/// pre-P0-1 stdout-only behaviour rather than crashing on boot because
/// of a malformed TOML.
fn preload_file_logging_config() -> Option<FileLoggingConfig> {
    let path = std::env::var("CORLINMAN_CONFIG")
        .ok()
        .map(PathBuf::from)
        .unwrap_or_else(Config::default_path);
    if !path.exists() {
        return None;
    }
    match Config::load_from_path(&path) {
        Ok(cfg) => Some(cfg.logging.file),
        Err(_) => None,
    }
}

fn resolve_addr() -> SocketAddr {
    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(6005);
    // Default to 127.0.0.1 for safety on developer laptops. Containerised
    // deploys set `BIND=0.0.0.0` so docker port-publishing actually reaches
    // the listener (docker-proxy dials 0.0.0.0:PORT inside the netns).
    let bind = std::env::var("BIND").unwrap_or_else(|_| "127.0.0.1".to_string());
    let ip: std::net::IpAddr = bind
        .parse()
        .unwrap_or_else(|_| std::net::IpAddr::from([127, 0, 0, 1]));
    SocketAddr::new(ip, port)
}

/// Best-effort boot of the `EvolutionObserver` (Phase 2 wave 1-A). Reads
/// `[evolution.observer]` from the same config the rest of the gateway
/// uses; missing config / `enabled = false` / DB open failures all skip
/// the spawn and return `None` so the gateway boots unchanged.
///
/// Returns the writer task handle so `main` can await its drain on
/// shutdown.
async fn maybe_spawn_evolution_observer(
    hook_bus: &corlinman_hooks::HookBus,
) -> Option<tokio::task::JoinHandle<()>> {
    let cfg = match load_config() {
        Ok(Some(cfg)) => cfg.evolution.observer,
        Ok(None) => corlinman_core::config::EvolutionObserverConfig::default(),
        Err(err) => {
            tracing::warn!(
                error = %err,
                "evolution observer: config load failed; using defaults",
            );
            corlinman_core::config::EvolutionObserverConfig::default()
        }
    };
    if !cfg.enabled {
        tracing::info!("evolution observer disabled by config");
        return None;
    }
    if let Some(parent) = cfg.db_path.parent() {
        if !parent.as_os_str().is_empty() {
            if let Err(err) = std::fs::create_dir_all(parent) {
                tracing::warn!(
                    error = %err,
                    dir = %parent.display(),
                    "evolution observer: could not create db dir; observer disabled",
                );
                return None;
            }
        }
    }
    let store = match EvolutionStore::open(&cfg.db_path).await {
        Ok(s) => s,
        Err(err) => {
            tracing::warn!(
                error = %err,
                path = %cfg.db_path.display(),
                "evolution observer: could not open evolution.sqlite; observer disabled",
            );
            return None;
        }
    };
    let repo = SignalsRepo::new(store.pool().clone());
    tracing::info!(
        path = %cfg.db_path.display(),
        queue_capacity = cfg.queue_capacity,
        "evolution observer: spawned"
    );
    Some(evolution_observer::spawn(
        Arc::new(hook_bus.clone()),
        repo,
        &cfg,
    ))
}

/// Resolve `$CORLINMAN_CONFIG` (or [`Config::default_path`]) and — when the
/// file exists and parses — spawn a [`ConfigWatcher`] bound to the shared
/// [`corlinman_hooks::HookBus`]. Returns the `(watcher, join handle)` pair so
/// `main` can both share the watcher with the router (for
/// `POST /admin/config/reload`) and await the background task on shutdown.
///
/// A missing / unreadable file yields `None`: the gateway still boots with
/// an `Arc<ArcSwap<Config>>` created inside `build_runtime_full`, and
/// `/admin/config/reload` returns 503 `config_reload_disabled`.
fn spawn_config_watcher(
    hook_bus: &corlinman_hooks::HookBus,
    cancel: CancellationToken,
) -> Option<(
    Arc<ConfigWatcher>,
    tokio::task::JoinHandle<anyhow::Result<()>>,
)> {
    let path = std::env::var("CORLINMAN_CONFIG")
        .ok()
        .map(PathBuf::from)
        .unwrap_or_else(Config::default_path);
    if !path.exists() {
        tracing::debug!(
            path = %path.display(),
            "config watcher: no file on disk; hot-reload disabled",
        );
        return None;
    }
    let initial = match Config::load_from_path(&path) {
        Ok(c) => c,
        Err(err) => {
            tracing::warn!(
                error = %err,
                path = %path.display(),
                "config watcher: initial load failed; hot-reload disabled",
            );
            return None;
        }
    };
    let watcher = Arc::new(ConfigWatcher::new(
        path,
        initial,
        Arc::new(hook_bus.clone()),
    ));
    let task = {
        let watcher = watcher.clone();
        tokio::spawn(async move { watcher.run(cancel).await })
    };
    tracing::info!("config watcher: spawned");
    Some((watcher, task))
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

/// Conditionally mount the Telegram webhook route on top of `router`. Skips
/// silently when the config isn't loaded, `public_url` is empty, the
/// `bot_token` is absent, or the token doesn't resolve (env var missing).
///
/// On webhook mode the bot's identity (`getMe`) is fetched once at boot so
/// the handler can classify group mentions without round-tripping Telegram
/// on every update. Boot-time `getMe` failures log and demote the route to
/// unmounted — the operator's next move is fixing the token and restarting.
///
/// Returns the (possibly unmodified) router.
async fn maybe_mount_telegram_webhook(
    router: axum::Router,
    hook_bus: corlinman_hooks::HookBus,
) -> axum::Router {
    use corlinman_channels::telegram::media::ReqwestHttp;
    use corlinman_gateway::routes::channels::{router_with_state, TelegramWebhookState};

    let cfg = match load_config() {
        Ok(Some(c)) => c,
        _ => return router,
    };
    let webhook_cfg = &cfg.telegram.webhook;
    if webhook_cfg.public_url.trim().is_empty() {
        tracing::debug!("telegram.webhook.public_url empty; webhook route not mounted");
        return router;
    }
    let Some(tg_cfg) = cfg.channels.telegram.as_ref() else {
        tracing::warn!("telegram.webhook.public_url set but channels.telegram missing");
        return router;
    };
    let token = match tg_cfg.bot_token.as_ref().and_then(|t| t.resolve().ok()) {
        Some(t) => t,
        None => {
            tracing::warn!("telegram.webhook.public_url set but bot_token not resolvable");
            return router;
        }
    };

    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()
    {
        Ok(c) => c,
        Err(err) => {
            tracing::warn!(error = %err, "telegram webhook: failed to build reqwest client");
            return router;
        }
    };

    // Fetch bot identity so classify() has a username to match against.
    let (bot_id, bot_username) = match fetch_bot_identity(&client, &token).await {
        Ok(v) => v,
        Err(err) => {
            tracing::warn!(error = %err, "telegram webhook: getMe failed; route not mounted");
            return router;
        }
    };

    let data_dir = std::env::var("CORLINMAN_DATA_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            dirs::home_dir()
                .map(|h| h.join(".corlinman"))
                .unwrap_or_else(|| PathBuf::from(".corlinman"))
        });

    let http: Arc<dyn corlinman_channels::telegram::media::TelegramHttp> =
        Arc::new(ReqwestHttp::new(client, token));

    let state = Arc::new(TelegramWebhookState {
        secret_token: webhook_cfg.secret_token.clone(),
        bot_id,
        bot_username,
        data_dir,
        http,
        hooks: Some(hook_bus),
    });

    tracing::info!(
        public_url = %webhook_cfg.public_url,
        bot_id,
        secret_configured = !webhook_cfg.secret_token.is_empty(),
        "telegram webhook route mounted"
    );

    // TODO(B4-BE1): also call Telegram's setWebhook at boot and
    // deleteWebhook at shutdown. Left as a follow-up in the same task so
    // the initial route lands without coupling boot to Telegram's API
    // availability (a network hiccup shouldn't block gateway startup).
    router.merge(router_with_state(state))
}

/// One-shot `getMe` call used during webhook-mode boot. Returns
/// `(bot_id, bot_username)`.
async fn fetch_bot_identity(
    client: &reqwest::Client,
    token: &str,
) -> anyhow::Result<(i64, Option<String>)> {
    #[derive(serde::Deserialize)]
    struct Env {
        ok: bool,
        #[serde(default)]
        description: Option<String>,
        #[serde(default)]
        result: Option<User>,
    }
    #[derive(serde::Deserialize)]
    struct User {
        id: i64,
        #[serde(default)]
        username: Option<String>,
    }
    let url = format!("https://api.telegram.org/bot{token}/getMe");
    let resp = client.get(&url).send().await?.error_for_status()?;
    let env: Env = resp.json().await?;
    if !env.ok {
        anyhow::bail!("getMe failed: {}", env.description.unwrap_or_default());
    }
    let u = env
        .result
        .ok_or_else(|| anyhow::anyhow!("getMe returned no result"))?;
    Ok((u.id, u.username))
}
