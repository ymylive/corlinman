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
use corlinman_evolution::{EvolutionStore, HistoryRepo, ProposalsRepo};
use corlinman_plugins::{roots_from_env_var, Origin, PluginRegistry, SearchRoot};
use corlinman_tenant::{AdminDb, TenantId, TenantPool};
use corlinman_vector::SqliteStore;
use tokio::net::TcpListener;
use tokio::sync::broadcast;

use crate::log_broadcast::LogRecord;
use crate::metrics;
use crate::middleware::admin_auth::AdminAuthState;
use crate::middleware::admin_session::AdminSessionStore;
use crate::middleware::approval::ApprovalGate;
use crate::middleware::trace;
use crate::routes;
use crate::routes::admin::scheduler::SchedulerHistory;
use crate::routes::admin::{self as admin_routes, AdminState};
use crate::routes::canvas::CanvasState;
use crate::routes::chat::{grpc::GrpcBackend, ChatBackend, ChatState};
use crate::routes::HealthState;

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

/// Variant of [`build_router_with_backend_registry_and_sessions`] that
/// swaps the stub `/health` route for one backed by live probes.
pub fn build_router_with_backend_registry_sessions_and_health(
    backend: Arc<dyn ChatBackend>,
    registry: Arc<PluginRegistry>,
    session_store: Arc<dyn SessionStore>,
    session_max_messages: usize,
    health_state: HealthState,
) -> Router {
    metrics::init();
    let async_tasks = registry.async_tasks();
    let state = ChatState::with_registry(backend, registry)
        .with_session_store(session_store)
        .with_session_max_messages(session_max_messages);
    trace::layer(routes::router_with_full_state_and_health(
        state,
        async_tasks,
        health_state,
    ))
}

/// Variant of [`build_router_with_backend_and_registry`] with real probes
/// on `/health`. Used when no session store is available.
pub fn build_router_with_backend_registry_and_health(
    backend: Arc<dyn ChatBackend>,
    registry: Arc<PluginRegistry>,
    health_state: HealthState,
) -> Router {
    metrics::init();
    let async_tasks = registry.async_tasks();
    let state = ChatState::with_registry(backend, registry);
    trace::layer(routes::router_with_full_state_and_health(
        state,
        async_tasks,
        health_state,
    ))
}

/// Default idle TTL for admin web sessions — 24h. Mirrors
/// `routes::admin::auth::DEFAULT_SESSION_TTL_SECS`.
const DEFAULT_ADMIN_SESSION_TTL_SECS: u64 = 86_400;

/// How long a `mode = "prompt"` tool call parks waiting for a human
/// decision before `ApprovalGate` auto-denies. 5 minutes matches the
/// gate's test-path default and is surfaced via the admin UI timer.
const DEFAULT_APPROVAL_TIMEOUT_SECS: u64 = 300;

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
///
/// Since S7.T5 the boot path uses [`build_admin_state_with_config`] so the
/// admin surface and `/health` share one `Arc<ArcSwap<Config>>`.
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
    build_runtime_with_logs_and_bus(None, None).await
}

/// Variant of [`build_runtime`] that also threads a `log_tx` sender
/// (produced by [`init_tracing`] in `main.rs`) into the admin state so
/// `/admin/logs/stream` can subscribe fresh receivers. Passing `None`
/// preserves the previous behaviour (the endpoint returns 503).
pub async fn build_runtime_with_logs(
    log_tx: Option<broadcast::Sender<LogRecord>>,
) -> (Router, Option<Arc<dyn ChatBackend>>, Arc<PluginRegistry>) {
    build_runtime_with_logs_and_bus(log_tx, None).await
}

/// B4-BE6: variant that additionally threads the shared
/// [`corlinman_hooks::HookBus`] into crate-internal constructors (notably
/// [`ApprovalGate`]). `None` for `hook_bus` preserves pre-B4-BE6 wiring
/// byte-for-byte; `Some` makes approval lifecycle events fan out to bus
/// subscribers on top of the existing SSE broadcaster.
pub async fn build_runtime_with_logs_and_bus(
    log_tx: Option<broadcast::Sender<LogRecord>>,
    hook_bus: Option<Arc<corlinman_hooks::HookBus>>,
) -> (Router, Option<Arc<dyn ChatBackend>>, Arc<PluginRegistry>) {
    let (router, backend, registry, _cfg, _cfg_path) =
        build_runtime_full(log_tx, hook_bus, None).await;
    (router, backend, registry)
}

/// B5-BE3: variant that also hands back the `Arc<ArcSwap<Config>>` and
/// on-disk config path so `main.rs` can attach a [`ConfigWatcher`] onto the
/// same live handle every admin / canvas / health subsystem already reads
/// from. `watcher` is optional — when `Some`, it is installed on
/// `admin_state.config_watcher` so `POST /admin/config/reload` works; the
/// caller still owns the watcher task and is responsible for spawning it.
pub async fn build_runtime_full(
    log_tx: Option<broadcast::Sender<LogRecord>>,
    hook_bus: Option<Arc<corlinman_hooks::HookBus>>,
    watcher: Option<Arc<crate::config_watcher::ConfigWatcher>>,
) -> (
    Router,
    Option<Arc<dyn ChatBackend>>,
    Arc<PluginRegistry>,
    Arc<ArcSwap<Config>>,
    Option<PathBuf>,
) {
    build_runtime_full_with_evolution(log_tx, hook_bus, watcher, None).await
}

/// Wave 1-C: superset of [`build_runtime_full`] that additionally accepts
/// the shared `EvolutionStore` opened by `main.rs` for the observer. When
/// `Some`, the resulting `AdminState` carries it through so
/// `/admin/evolution/*` returns real data; `None` keeps the previous
/// behaviour (those routes 503 with `evolution_disabled`). All earlier
/// signatures stay backwards-compatible — `build_runtime_full` just calls
/// this with `evolution_store = None`.
pub async fn build_runtime_full_with_evolution(
    log_tx: Option<broadcast::Sender<LogRecord>>,
    hook_bus: Option<Arc<corlinman_hooks::HookBus>>,
    watcher: Option<Arc<crate::config_watcher::ConfigWatcher>>,
    evolution_store: Option<Arc<EvolutionStore>>,
) -> (
    Router,
    Option<Arc<dyn ChatBackend>>,
    Arc<PluginRegistry>,
    Arc<ArcSwap<Config>>,
    Option<PathBuf>,
) {
    let registry = Arc::new(load_plugin_registry());
    tracing::info!(
        plugin_count = registry.len(),
        diagnostic_count = registry.diagnostics().len(),
        "plugin registry loaded",
    );

    // Resolve config *once* so both the admin state and the /health probe
    // share the same `Arc<ArcSwap<Config>>` — live reloads propagate to
    // both surfaces at once. B5-BE3: when the caller passed in a
    // `ConfigWatcher`, reuse its `ArcSwap` so a hot-reload lands in every
    // subsystem that reads from this handle without a second swap.
    let (cfg, cfg_path) = load_admin_config();
    let config_handle: Arc<ArcSwap<Config>> = match watcher.as_ref() {
        Some(w) => w.arc_swap(),
        None => Arc::new(ArcSwap::from_pointee(cfg)),
    };

    // Phase 4 W1 4-1A Item 5: rename legacy `<data_dir>/<name>.sqlite`
    // files into `<data_dir>/tenants/default/<name>.sqlite` BEFORE any
    // store is opened. Otherwise `open_session_store` and friends
    // would happily open the legacy paths and the migration would
    // race / be impossible mid-run. Idempotent and gated; see
    // `crate::legacy_migration` for the rules.
    {
        let snap = config_handle.load();
        if snap.tenants.enabled && snap.tenants.migrate_legacy_paths {
            if let Err(err) =
                crate::legacy_migration::migrate_legacy_data_files(&resolve_data_dir())
            {
                tracing::error!(
                    error = %err,
                    "phase 4 legacy data file migration failed; \
                     gateway will continue but per-tenant routes may 503",
                );
            }
        }
    }

    // Open the session history store, keyed off `$CORLINMAN_DATA_DIR`.
    let session_store = open_session_store().await;

    // S7.T5: bundle the health probe state.
    let endpoint = resolve_endpoint();
    let health_state = HealthState {
        config: Some(config_handle.clone()),
        data_dir: Some(resolve_data_dir()),
        plugin_registry: Some(registry.clone()),
        agent_endpoint: Some(endpoint.clone()),
    };

    let (base_router, backend_opt) = match connect_channel(&endpoint).await {
        Ok(channel) => {
            tracing::info!(endpoint = %endpoint, "agent client connected");
            let client = AgentClient::new(channel);
            let backend: Arc<dyn ChatBackend> = Arc::new(GrpcBackend::new(client));
            metrics::init();
            let async_tasks = registry.async_tasks();
            let mut state = ChatState::with_registry(backend.clone(), registry.clone())
                .with_live_model_config(config_handle.clone());
            if let Some(store) = session_store {
                state = state
                    .with_session_store(store)
                    .with_session_max_messages(DEFAULT_SESSION_MAX_MESSAGES);
            }
            let router = trace::layer(routes::router_with_full_state_and_health(
                state,
                async_tasks,
                health_state.clone(),
            ));
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
    //
    // Post-S6 wire-up: open the RAG SQLite at `<data_dir>/kb.sqlite` so
    // `/admin/rag/*` has real data to read, and construct a fresh
    // in-memory `SchedulerHistory` so `/admin/scheduler/history` has a
    // sink for records when the cron runtime lands in M7.
    //
    // 0.1.4 wire-up: when the SQLite store opened (it carries the
    // `pending_approvals` table since vector migration v3), also
    // construct an `ApprovalGate` sourced from the current config so
    // `/admin/approvals*` stop returning 503 `approvals_disabled`.
    let rag_store = open_rag_store().await;
    let scheduler_history = Some(SchedulerHistory::new());
    let approval_gate = rag_store.as_ref().map(|store| {
        let mut gate = ApprovalGate::new(
            config_handle.load().approvals.rules.clone(),
            store.clone(),
            StdDuration::from_secs(DEFAULT_APPROVAL_TIMEOUT_SECS),
        );
        // B4-BE6: mirror approval lifecycle to the unified bus if the
        // caller supplied one. Additive — the gate's own
        // `broadcast::Sender<ApprovalEvent>` still drives
        // `/admin/approvals/stream`.
        if let Some(bus) = hook_bus.as_ref() {
            gate = gate.with_bus(bus.clone());
        }
        Arc::new(gate)
    });
    // Feature C last-mile: drop the Python-side JSON config so the
    // `ProviderRegistry` / `EmbeddingSpec` subprocess boots with the
    // full provider / alias / embedding map. The env var `CORLINMAN_PY_CONFIG`
    // points the Python side at the file; exporting it here means every
    // child process (including any in-code `Command::spawn` that lands
    // later) inherits it. Supervisors that launch the Python server in a
    // sibling container should set the same env via `docker/start.sh`.
    let py_config_path = crate::py_config::default_py_config_path();
    let config_snapshot = config_handle.load_full();
    match crate::py_config::write_py_config_sync(&config_snapshot, &py_config_path) {
        Ok(()) => {
            std::env::set_var(crate::py_config::ENV_PY_CONFIG, &py_config_path);
            tracing::info!(
                path = %py_config_path.display(),
                env = crate::py_config::ENV_PY_CONFIG,
                "py-config: exported for python subprocess",
            );
        }
        Err(err) => {
            tracing::warn!(
                error = %err,
                path = %py_config_path.display(),
                "py-config: initial write failed; python will fall back to legacy prefix table",
            );
        }
    }

    let mut admin_state = build_admin_state_with_config(
        registry.clone(),
        log_tx,
        rag_store,
        scheduler_history,
        approval_gate,
        config_handle.clone(),
        cfg_path.clone(),
        Some(py_config_path),
    )
    .await;
    if let Some(w) = watcher.as_ref() {
        admin_state = admin_state.with_config_watcher(w.clone());
    }
    if let Some(backend) = backend_opt.as_ref() {
        let replay_service: Arc<dyn corlinman_gateway_api::ChatService> =
            Arc::new(crate::services::ChatService::new(backend.clone()));
        admin_state = admin_state.with_replay_chat_service(replay_service);
    }
    if let Some(store) = evolution_store {
        // Wave 2-A: when both the evolution store and the kb store are
        // available, build a real `EvolutionApplier` so
        // `POST /admin/evolution/:id/apply` mutates kb.sqlite + writes
        // an `evolution_history` row. Missing kb store → applier stays
        // `None`, the route 503s alongside the rest of the evolution
        // surface so the UI keeps a single banner.
        if let Some(kb) = admin_state.rag_store.clone() {
            // W1-B: applier needs the AutoRollback thresholds so
            // `metrics_baseline` is captured over the configured
            // signal window. Snapshot via `load()` — a hot-reload
            // mid-apply is racy in a way the snapshot wouldn't fix
            // anyway, and the next apply picks up the new value.
            let snapshot = config_handle.load();
            let thresholds = snapshot.evolution.auto_rollback.thresholds.clone();
            // Phase 3-2B: skill_update proposals resolve `skills/...`
            // targets under `<data_dir>/<[skills].dir>`. Snapshot at
            // boot — same reasoning as `thresholds` above.
            let skills_dir = resolve_data_dir().join(&snapshot.skills.dir);
            // Phase 4 W2 B1 iter 5: thread the operator-only meta-approver
            // allow-list from `[admin].meta_approver_users` so the applier's
            // authoritative gate fires on every meta apply path. Empty list
            // (the safe default) blocks every meta apply with
            // `MetaApproverRequired` until the operator opts in.
            let meta_approvers = snapshot.admin.meta_approver_users.clone();
            let applier = Arc::new(
                crate::evolution_applier::EvolutionApplier::new(
                    store.clone(),
                    kb,
                    thresholds,
                    skills_dir,
                )
                .with_meta_approver_users(meta_approvers),
            );
            admin_state = admin_state.with_evolution_applier(applier);
        } else {
            tracing::warn!(
                "evolution applier not constructed: kb store missing; \
                 /admin/evolution/:id/apply will return 503",
            );
        }
        // Phase 3.1: hand the same pool to the manual-decay-reset
        // admin route via `HistoryRepo` + `ProposalsRepo`. Both are
        // pool-clone wrappers, so this doesn't widen the connection
        // budget.
        let history_repo = HistoryRepo::new(store.pool().clone());
        let proposals_repo = ProposalsRepo::new(store.pool().clone());
        admin_state = admin_state.with_history_repo(history_repo, proposals_repo);
        admin_state = admin_state.with_evolution_store(store);
    }
    // B5-BE1: Canvas Host protocol stubs. Sub-router carries its own auth
    // guard (shares `AdminAuthState` with the admin surface), so we can
    // merge it alongside the admin router without widening the public
    // namespace. The routes return 503 when `[canvas] host_endpoint_enabled
    // = false` (default) — see `routes::canvas`.
    let canvas_auth_state = {
        let mut s = AdminAuthState::new(config_handle.clone());
        if let Some(store) = admin_state.session_store.as_ref() {
            s = s.with_session_store(store.clone());
        }
        s
    };
    let canvas_state = CanvasState::new(config_handle.clone());
    let base_router = base_router.merge(routes::canvas::router(canvas_state, canvas_auth_state));
    let router = mount_admin_routes(base_router, admin_state);

    (router, backend_opt, registry, config_handle, cfg_path)
}

/// Variant of [`build_admin_state`] that reuses a pre-loaded config handle
/// so boot code can share the same live-reload swap with `/health`.
#[allow(clippy::too_many_arguments)]
async fn build_admin_state_with_config(
    plugins: Arc<PluginRegistry>,
    log_tx: Option<broadcast::Sender<LogRecord>>,
    rag_store: Option<Arc<SqliteStore>>,
    scheduler_history: Option<Arc<SchedulerHistory>>,
    approval_gate: Option<Arc<ApprovalGate>>,
    config_handle: Arc<ArcSwap<Config>>,
    cfg_path: Option<PathBuf>,
    py_config_path: Option<PathBuf>,
) -> AdminState {
    let session_store = Arc::new(AdminSessionStore::new(StdDuration::from_secs(
        DEFAULT_ADMIN_SESSION_TTL_SECS,
    )));
    let cancel = tokio_util::sync::CancellationToken::new();
    let _handle = Arc::clone(&session_store).start_gc(cancel);

    let mut admin = AdminState::new(plugins, config_handle).with_session_store(session_store);
    if let Some(path) = cfg_path {
        admin = admin.with_config_path(path);
    }
    if let Some(tx) = log_tx {
        admin = admin.with_log_broadcast(tx);
    }
    if let Some(store) = rag_store {
        admin = admin.with_rag_store(store);
    }
    if let Some(history) = scheduler_history {
        admin = admin.with_scheduler_history(history);
    }
    if let Some(gate) = approval_gate {
        admin = admin.with_approval_gate(gate);
    }
    if let Some(path) = py_config_path {
        admin = admin.with_py_config_path(path);
    }

    // Phase 4 W1 4-1A: when `[tenants].enabled = true`, construct the
    // multi-tenant SQLite pool and the operator-allowed tenant set.
    // Both are consumed by the tenant-scoping middleware (Item 3) which
    // routes admin requests through the per-tenant SQLite layout under
    // `<data_dir>/tenants/<tenant_id>/`. When `enabled = false` (legacy
    // single-tenant default) we leave both fields untouched: the
    // `AdminState::default` shape is `tenant_pool = None,
    // allowed_tenants = empty`, which the middleware reads as "no
    // scoping, fall back to legacy unscoped DB paths".
    let cfg_snap = admin.config.load();
    if cfg_snap.tenants.enabled {
        let data_dir = resolve_data_dir();
        let pool = Arc::new(TenantPool::new(data_dir.clone()));

        // Build the allowed-tenants set from `[tenants].allowed` plus
        // the implicit reserved `default` slug. Invalid slugs are
        // tracing::warn'd and dropped; the remaining set is what
        // operators can hit. Future Item 4 will fold in tenants.sqlite
        // rows here; for now config is the only source.
        let mut allowed = std::collections::BTreeSet::new();
        allowed.insert(TenantId::legacy_default());
        for slug in &cfg_snap.tenants.allowed {
            match TenantId::new(slug) {
                Ok(t) => {
                    allowed.insert(t);
                }
                Err(e) => {
                    tracing::warn!(
                        slug = %slug,
                        error = %e,
                        "[tenants].allowed entry rejected; skipping",
                    );
                }
            }
        }

        tracing::info!(
            data_dir = %data_dir.display(),
            allowed_count = allowed.len(),
            "multi-tenant mode enabled; tenant pool and allowlist installed",
        );

        admin = admin.with_tenant_pool(pool).with_allowed_tenants(allowed);

        // Phase 4 W1 4-1B: also open the root-level `tenants.sqlite`
        // admin DB so `/admin/tenants*` has a real backing store. A
        // failure here doesn't abort boot — the gateway keeps serving
        // and the routes return 503 `tenants_disabled` +
        // `reason=admin_db_missing` so operators see a clear "DB
        // unreachable" envelope instead of a silent 500.
        let admin_db_path = data_dir.join("tenants.sqlite");
        match AdminDb::open(&admin_db_path).await {
            Ok(db) => {
                tracing::info!(
                    path = %admin_db_path.display(),
                    "admin tenants.sqlite opened",
                );
                admin = admin.with_admin_db(Arc::new(db));
            }
            Err(err) => {
                tracing::warn!(
                    path = %admin_db_path.display(),
                    error = %err,
                    "could not open admin tenants.sqlite; /admin/tenants* will return 503",
                );
            }
        }
    }

    admin
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

/// Resolve `<data_dir>/kb.sqlite` and open (or create) the RAG SQLite store.
/// Returns `None` when opening fails — the gateway keeps serving and
/// `/admin/rag/*` returns 503 `rag_disabled` so boot stays resilient on
/// fresh installs / read-only data dirs.
async fn open_rag_store() -> Option<Arc<SqliteStore>> {
    let data_dir = resolve_data_dir();
    let path = data_dir.join("kb.sqlite");
    if let Some(parent) = path.parent() {
        if let Err(err) = std::fs::create_dir_all(parent) {
            tracing::warn!(
                dir = %parent.display(),
                error = %err,
                "could not create rag data dir; rag admin disabled",
            );
            return None;
        }
    }
    match SqliteStore::open(&path).await {
        Ok(store) => {
            tracing::info!(path = %path.display(), "rag store opened");
            Some(Arc::new(store))
        }
        Err(err) => {
            tracing::warn!(
                path = %path.display(),
                error = %err,
                "could not open rag store; rag admin disabled",
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
