//! axum Router construction + HTTP server bootstrap.
//!
//! Later milestones fold the tonic gRPC server (VectorService + PluginBridge)
//! into this same entry point; this first revision only wires axum.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration as StdDuration;

use arc_swap::ArcSwap;
use axum::Router;
use corlinman_agent_client::client::{connect_channel, resolve_endpoint, AgentClient};
use corlinman_core::config::Config;
use corlinman_core::{SessionStore, SqliteSessionStore};
use corlinman_plugins::{roots_from_env_var, Origin, PluginRegistry, SearchRoot};
use tokio::net::TcpListener;
use tokio::sync::broadcast;

use crate::log_broadcast::LogRecord;
use crate::metrics;
use crate::middleware::admin_session::AdminSessionStore;
use crate::middleware::trace;
use crate::routes;
use crate::routes::admin::{self as admin_routes, AdminState};
use crate::routes::chat::{grpc::GrpcBackend, ChatBackend, ChatState};

/// Build the top-level axum router with the default (stub) chat route.
///
/// Returns 501 for `/v1/chat/completions` — use [`build_router_with_backend`]
/// to wire the real gRPC backend.
pub fn build_router() -> Router {
    metrics::init();
    trace::layer(routes::router())
}

/// Build the router with a concrete [`ChatBackend`]. Used both by `main` and
/// by integration tests that want a running handler.
///
/// Uses the M2 placeholder tool executor — suitable for tests that don't
/// care about plugin execution. Production boot goes through
/// [`build_router_for_runtime`] which loads a real [`PluginRegistry`].
pub fn build_router_with_backend(backend: Arc<dyn ChatBackend>) -> Router {
    metrics::init();
    let state = ChatState::new(backend);
    trace::layer(routes::router_with_chat_state(state))
}

/// Build the router with a backend and a plugin registry so the chat route
/// dispatches `ToolCall` frames to real plugin processes, and
/// `/plugin-callback/:task_id` resolves async plugin parked tool_calls via
/// the registry's shared [`AsyncTaskRegistry`].
pub fn build_router_with_backend_and_registry(
    backend: Arc<dyn ChatBackend>,
    registry: Arc<PluginRegistry>,
) -> Router {
    metrics::init();
    let async_tasks = registry.async_tasks();
    let state = ChatState::with_registry(backend, registry);
    trace::layer(routes::router_with_full_state(state, async_tasks))
}

/// Same as [`build_router_with_backend_and_registry`] but also attaches a
/// session store so `/v1/chat/completions` persists and re-hydrates per-session
/// message histories. `session_max_messages` is the post-turn trim cap.
pub fn build_router_with_backend_registry_and_sessions(
    backend: Arc<dyn ChatBackend>,
    registry: Arc<PluginRegistry>,
    session_store: Arc<dyn SessionStore>,
    session_max_messages: usize,
) -> Router {
    metrics::init();
    let async_tasks = registry.async_tasks();
    let state = ChatState::with_registry(backend, registry)
        .with_session_store(session_store)
        .with_session_max_messages(session_max_messages);
    trace::layer(routes::router_with_full_state(state, async_tasks))
}

/// Default idle TTL for admin web sessions — 24h. Mirrors
/// `routes::admin::auth::DEFAULT_SESSION_TTL_SECS`.
const DEFAULT_ADMIN_SESSION_TTL_SECS: u64 = 86_400;

/// Build the `AdminState` for the admin REST routes. Loads config from
/// `$CORLINMAN_CONFIG` (same logic as `main.rs`), attaches a brand-new
/// [`AdminSessionStore`], and spawns a detached GC task for it.
///
/// The GC task is *not* cancellable from here — it lives for the process
/// lifetime. That's fine: tokio aborts background tasks on runtime drop,
/// and `main.rs` does `std::process::exit` on SIGTERM so nothing leaks.
///
/// When `$CORLINMAN_CONFIG` points at a real file the resolved path is
/// attached via [`AdminState::with_config_path`] so `POST /admin/config`
/// can persist accepted payloads back to the same file at runtime.
fn build_admin_state(
    plugins: Arc<PluginRegistry>,
    log_tx: Option<broadcast::Sender<LogRecord>>,
) -> AdminState {
    let (cfg, cfg_path) = load_admin_config();
    let session_store = Arc::new(AdminSessionStore::new(StdDuration::from_secs(
        DEFAULT_ADMIN_SESSION_TTL_SECS,
    )));

    // Fire-and-forget GC. Uses a fresh CancellationToken that never fires,
    // so the task loops until the process exits.
    let cancel = tokio_util::sync::CancellationToken::new();
    let _handle = Arc::clone(&session_store).start_gc(cancel);

    let mut admin = AdminState::new(plugins, Arc::new(ArcSwap::from_pointee(cfg)))
        .with_session_store(session_store);
    if let Some(path) = cfg_path {
        admin = admin.with_config_path(path);
    }
    if let Some(tx) = log_tx {
        admin = admin.with_log_broadcast(tx);
    }
    admin
}

/// Same `$CORLINMAN_CONFIG` lookup `main.rs` uses. Missing / unreadable →
/// `(Config::default(), None)` so the gateway still boots (admin endpoints
/// then return 503 until credentials land in config). When a file was
/// successfully read the resolved path is returned so the admin state can
/// persist subsequent live-reload writes back to it.
fn load_admin_config() -> (Config, Option<PathBuf>) {
    let Ok(path_str) = std::env::var("CORLINMAN_CONFIG") else {
        return (Config::default(), None);
    };
    let path = PathBuf::from(path_str);
    if !path.exists() {
        return (Config::default(), None);
    }
    match Config::load_from_path(&path) {
        Ok(cfg) => (cfg, Some(path)),
        Err(err) => {
            tracing::warn!(error = %err, "admin config load failed; using defaults");
            (Config::default(), None)
        }
    }
}

/// Mount the admin sub-router produced by
/// [`crate::routes::admin::router_with_state`] onto the base gateway router.
/// Done as a separate merge so the admin routes sit behind the session +
/// basic-auth guard while the rest of the gateway stays public (per-route
/// guards live inside their own modules).
fn mount_admin_routes(base: Router, state: AdminState) -> Router {
    base.merge(admin_routes::router_with_state(state))
}

/// Connect to the Python gRPC agent server; falls back to the stub router
/// when the agent isn't reachable (so `/health` stays up even if Python died).
pub async fn build_router_for_runtime() -> Router {
    let (router, _, _) = build_runtime().await;
    router
}

/// Same as [`build_router_for_runtime`] but also returns the shared
/// [`ChatBackend`] when the agent was reachable, plus the live
/// [`PluginRegistry`] so callers (e.g. `main`) can spawn a hot reloader on
/// top of it.
///
/// Opens `<data_dir>/sessions.sqlite` lazily; a failure there only warns and
/// the gateway boots without session history (falls back to stateless single
/// turns). This keeps boot resilient on first run / fresh containers.
pub async fn build_runtime() -> (Router, Option<Arc<dyn ChatBackend>>, Arc<PluginRegistry>) {
    build_runtime_with_logs(None).await
}

/// Variant of [`build_runtime`] that also threads a `log_tx` sender
/// (produced by [`init_tracing`] in `main.rs`) into the admin state so
/// `/admin/logs/stream` can subscribe fresh receivers. Passing `None`
/// preserves the previous behaviour (the endpoint returns 503).
pub async fn build_runtime_with_logs(
    log_tx: Option<broadcast::Sender<LogRecord>>,
) -> (Router, Option<Arc<dyn ChatBackend>>, Arc<PluginRegistry>) {
    let registry = Arc::new(load_plugin_registry());
    tracing::info!(
        plugin_count = registry.len(),
        diagnostic_count = registry.diagnostics().len(),
        "plugin registry loaded",
    );

    // Open the session history store, keyed off `$CORLINMAN_DATA_DIR`.
    let session_store = open_session_store().await;

    let endpoint = resolve_endpoint();
    let (base_router, backend_opt) = match connect_channel(&endpoint).await {
        Ok(channel) => {
            tracing::info!(endpoint = %endpoint, "agent client connected");
            let client = AgentClient::new(channel);
            let backend: Arc<dyn ChatBackend> = Arc::new(GrpcBackend::new(client));
            let router = match session_store {
                Some(store) => build_router_with_backend_registry_and_sessions(
                    backend.clone(),
                    registry.clone(),
                    store,
                    DEFAULT_SESSION_MAX_MESSAGES,
                ),
                None => build_router_with_backend_and_registry(backend.clone(), registry.clone()),
            };
            (router, Some(backend))
        }
        Err(err) => {
            tracing::warn!(
                endpoint = %endpoint,
                error = %err,
                "agent client unreachable; /v1/chat/completions will 501",
            );
            (build_router(), None)
        }
    };

    // S5 T1: wire the real admin REST routes + session store on top of
    // whatever base router we just built. The stub `admin::router()` that
    // `routes::router_with_full_state` merged returns 501 for every path;
    // merging a second time means both routers share the `/admin/*`
    // namespace. axum rejects duplicate route definitions, so we rely on
    // the stub being a wildcard (`/admin/*path`) — the concrete routes
    // here take precedence because axum matches specific paths before
    // wildcards. See `routes::admin::router()` for the stub.
    let admin_state = build_admin_state(registry.clone(), log_tx);
    let router = mount_admin_routes(base_router, admin_state);

    (router, backend_opt, registry)
}

/// Default session trim cap used when a config isn't loaded. Matches
/// `ServerConfig::default().session_max_messages`.
const DEFAULT_SESSION_MAX_MESSAGES: usize = 100;

/// Resolve `<data_dir>/sessions.sqlite` and open (or create) the store.
/// Returns `None` when opening fails — the gateway continues stateless rather
/// than refusing to boot.
async fn open_session_store() -> Option<Arc<dyn SessionStore>> {
    let data_dir = resolve_data_dir();
    let path = data_dir.join("sessions.sqlite");
    if let Some(parent) = path.parent() {
        if let Err(err) = std::fs::create_dir_all(parent) {
            tracing::warn!(
                dir = %parent.display(),
                error = %err,
                "could not create session data dir; sessions disabled",
            );
            return None;
        }
    }
    match SqliteSessionStore::open(&path).await {
        Ok(store) => {
            tracing::info!(path = %path.display(), "session store opened");
            Some(Arc::new(store) as Arc<dyn SessionStore>)
        }
        Err(err) => {
            tracing::warn!(
                path = %path.display(),
                error = %err,
                "could not open session store; sessions disabled",
            );
            None
        }
    }
}

/// Resolve the data directory the same way `main.rs` / plugins do: honour
/// `$CORLINMAN_DATA_DIR`, else fall back to `~/.corlinman`.
fn resolve_data_dir() -> PathBuf {
    if let Ok(dir) = std::env::var("CORLINMAN_DATA_DIR") {
        return PathBuf::from(dir);
    }
    dirs::home_dir()
        .map(|h| h.join(".corlinman"))
        .unwrap_or_else(|| PathBuf::from(".corlinman"))
}

/// Discover plugins from, in priority order:
///   1. `$CORLINMAN_DATA_DIR/plugins/` (user-installed),
///   2. each colon-separated entry in `$CORLINMAN_PLUGIN_EXTRA_DIRS`,
///   3. each colon-separated entry in `$CORLINMAN_PLUGIN_DIRS` (matches the CLI).
///
/// Missing directories are silently ignored so a fresh install boots cleanly.
fn load_plugin_registry() -> PluginRegistry {
    let mut roots: Vec<SearchRoot> = Vec::new();
    if let Ok(data_dir) = std::env::var("CORLINMAN_DATA_DIR") {
        let path = std::path::PathBuf::from(data_dir).join("plugins");
        roots.push(SearchRoot::new(path, Origin::Config));
    }
    roots.extend(roots_from_env_var(
        "CORLINMAN_PLUGIN_EXTRA_DIRS",
        Origin::Config,
    ));
    roots.extend(roots_from_env_var("CORLINMAN_PLUGIN_DIRS", Origin::Config));
    PluginRegistry::from_roots(roots)
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

/// Variant of [`run`] that accepts a prebuilt router (produced by
/// [`build_runtime`]). Used when `main` needs the backend handle for
/// side-by-side channel tasks.
pub async fn run_with_router<F>(addr: SocketAddr, router: Router, shutdown: F) -> anyhow::Result<()>
where
    F: std::future::Future<Output = ()> + Send + 'static,
{
    let listener = TcpListener::bind(addr).await?;
    tracing::info!(%addr, "gateway listening");
    axum::serve(listener, router)
        .with_graceful_shutdown(shutdown)
        .await?;
    Ok(())
}
