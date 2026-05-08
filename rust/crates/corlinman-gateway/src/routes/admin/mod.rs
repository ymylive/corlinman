//! `/admin/*` REST endpoints — narrow M6 scope.
//!
//! This module ships three read-only endpoints backed by real state:
//!   - `GET /admin/plugins`       — list registry entries
//!   - `GET /admin/plugins/:name` — manifest + diagnostics for one plugin
//!   - `GET /admin/agents`        — list `.md` files under `server.data_dir/agents/`
//!
//! All routes live behind [`crate::middleware::admin_auth::require_admin`]
//! (HTTP Basic for now — session / JWT lands in M7). Writes, SSE log streaming,
//! live config swap, and the `doctor` subcommand stay behind `not_implemented`
//! until their respective milestones.
//!
//! The legacy [`router`] (no args) still returns 501 for `/admin/*`, so the
//! existing [`crate::routes::router`] stays valid. Callers that can supply
//! real state should use [`router_with_state`] instead.

use std::collections::BTreeSet;
use std::path::PathBuf;
use std::sync::Arc;

use arc_swap::ArcSwap;
use axum::{routing::any, Router};
use corlinman_core::config::Config;
use corlinman_evolution::{EvolutionStore, HistoryRepo, ProposalsRepo};
use corlinman_identity::IdentityStore;
use corlinman_plugins::registry::PluginRegistry;
use corlinman_tenant::{AdminDb, TenantId, TenantPool};
use corlinman_vector::SqliteStore;
use tokio::sync::broadcast;

use crate::config_watcher::ConfigWatcher;
use crate::evolution_applier::EvolutionApplier;
use crate::log_broadcast::LogRecord;
use crate::middleware::admin_auth::{require_admin, AdminAuthState};
use crate::middleware::admin_session::AdminSessionStore;
use crate::middleware::approval::ApprovalGate;
use crate::middleware::tenant_scope::{tenant_scope, TenantScopeState};

use super::not_implemented;

pub mod agents;
pub mod approvals;
pub mod auth;
pub mod channels;
pub mod config;
pub mod embedding;
pub mod evolution;
pub mod federation;
pub mod logs;
pub mod memory;
pub mod models;
pub mod napcat;
pub mod plugins;
pub mod providers;
pub mod rag;
pub mod scheduler;
pub mod identity;
pub mod sessions;
pub mod tenants;

/// Shared read-only state passed to every admin handler.
///
/// Cloneable because every field is wrapped in `Arc`. Handlers load the
/// current snapshot via `state.plugins.clone()` or `state.config.load()`.
#[derive(Clone)]
pub struct AdminState {
    pub plugins: Arc<PluginRegistry>,
    pub config: Arc<ArcSwap<Config>>,
    /// Sprint 2 T3: handle used by `/admin/approvals*` routes to list,
    /// decide, and broadcast pending tool-approval requests. `None` on
    /// stripped-down builds that boot without approval rules configured;
    /// the `/admin/approvals*` endpoints then return 503.
    pub approval_gate: Option<Arc<ApprovalGate>>,
    /// Sprint 5 T1: session registry shared with `admin_auth` middleware.
    /// `None` on bare test harnesses that only exercise Basic-auth paths;
    /// `/admin/login`, `/admin/logout`, `/admin/me` then 503.
    pub session_store: Option<Arc<AdminSessionStore>>,
    /// Sprint 5 T2: on-disk location of the currently-loaded config.
    /// `POST /admin/config` re-serialises accepted payloads here via an
    /// atomic tmp-then-rename write. `None` in test harnesses that exercise
    /// the validation / swap path without a real file (the POST handler
    /// then returns 503 `config_path_unset`).
    pub config_path: Option<PathBuf>,
    /// Sprint 5 T3: broadcast sender fed by
    /// [`crate::log_broadcast::BroadcastLayer`]. `/admin/logs/stream`
    /// subscribes once per connection. `None` in stripped-down test
    /// harnesses that don't install the tracing layer; the endpoint
    /// then returns 503 `logs_disabled`.
    pub log_broadcast: Option<broadcast::Sender<LogRecord>>,
    /// Sprint 6 T1: SQLite handle for the RAG corpus. `None` on boots
    /// without the vector store (stripped-down test harness); the
    /// `/admin/rag/*` routes then return 503 `rag_disabled`.
    pub rag_store: Option<Arc<SqliteStore>>,
    /// Sprint 6 T3: in-memory scheduler run history. `None` until the
    /// cron runtime lands in M7 (see `corlinman-scheduler`); the
    /// admin routes degrade gracefully to empty-list.
    pub scheduler_history: Option<Arc<scheduler::SchedulerHistory>>,
    /// Feature C last-mile: path to the Python-side JSON config drop
    /// (`$CORLINMAN_DATA_DIR/py-config.json`). When set, every admin
    /// write that mutates providers / aliases / embedding re-serialises
    /// the active [`Config`] to this file so the Python subprocess picks
    /// up the new shape on its next resolve call. `None` on test harnesses
    /// that don't exercise the Python integration.
    pub py_config_path: Option<PathBuf>,
    /// B5-BE3: live config hot-reload handle. When set, `POST
    /// /admin/config/reload` calls `trigger_reload()` for manual reloads
    /// (useful in ops scripts / container healthchecks). `None` on test
    /// harnesses that don't spawn the watcher — the endpoint then 503s
    /// with `config_reload_disabled`.
    pub config_watcher: Option<Arc<ConfigWatcher>>,
    /// Wave 1-C: shared `evolution.sqlite` handle backing the
    /// `/admin/evolution/*` admin API. `None` when the EvolutionObserver
    /// failed to open the database (or `[evolution.observer.enabled]` =
    /// false); every `/admin/evolution/*` route then returns 503
    /// `evolution_disabled`, matching the approval-gate convention. The
    /// admin handlers build a fresh `ProposalsRepo` per request — that's
    /// just a pool-clone wrapper, so the cost is negligible and avoids a
    /// second field that has to stay in sync with the store.
    pub evolution_store: Option<Arc<EvolutionStore>>,
    /// Wave 2-A: real `EvolutionApplier` that mutates `kb.sqlite` and
    /// records `evolution_history` rows when an approved `memory_op`
    /// proposal is applied. `None` when either the kb store or the
    /// evolution store failed to open at boot — `POST /admin/evolution/
    /// :id/apply` then returns 503 `evolution_disabled` with the same
    /// shape `evolution_store=None` returns, so the UI can keep its
    /// single banner. Holding it on `AdminState` (rather than rebuilding
    /// per-request) keeps the kb-pool clones to one per gateway boot.
    pub evolution_applier: Option<Arc<EvolutionApplier>>,
    /// Phase 3.1: history+proposals repos used by `/admin/memory/decay/
    /// reset` to record the manual decay-reset as a synthetic
    /// `memory_op` row in `evolution_history` (with a paired proposal
    /// row to satisfy the FK). `None` when `evolution_store` is missing
    /// — the route then 503s alongside the rest of the evolution
    /// surface.
    pub history_repo: Option<HistoryRepo>,
    pub proposals_repo: Option<ProposalsRepo>,
    /// Phase 4 W1 4-1A: shared multi-tenant SQLite pool wrapper keyed
    /// by `(TenantId, db_name)`. `None` on legacy single-tenant boots
    /// where `[tenants].enabled = false` — the per-tenant routes then
    /// resolve every request to `TenantId::legacy_default()` and read
    /// from the legacy unscoped DB paths. `Some` only when the gateway
    /// constructed a multi-tenant pool at boot; the tenant-scoping
    /// middleware then routes admin requests through this pool.
    pub tenant_pool: Option<Arc<TenantPool>>,
    /// Phase 4 W1 4-1A: union of `[tenants].allowed` slugs from config
    /// and active rows in `tenants.sqlite`. The tenant-scoping
    /// middleware rejects any session claim or `?tenant=` query whose
    /// slug is not in this set with HTTP 403. Empty when
    /// `[tenants].enabled = false` — middleware short-circuits in that
    /// case before this set is consulted, so the empty default is safe.
    pub allowed_tenants: BTreeSet<TenantId>,
    /// Phase 4 W1 4-1B: handle to the root-level `tenants.sqlite` admin
    /// DB used by `/admin/tenants*` to list / create tenants. `None`
    /// when either `[tenants].enabled = false` (legacy single-tenant
    /// mode) or the boot-time `AdminDb::open` failed (read-only data
    /// dir, etc). The `/admin/tenants*` routes return 403
    /// `tenants_disabled` for the first case and 503
    /// `tenants_disabled` + `reason=admin_db_missing` for the second,
    /// matching the UI mock contract in `ui/lib/api/tenants.ts`.
    pub admin_db: Option<Arc<AdminDb>>,
    /// Phase 4 W2 4-2D: kill-switch for the `/admin/sessions*` admin
    /// surface. Defaults to `false` (sessions surface is on). When
    /// flipped to `true` — typically by a test harness or a future
    /// `[sessions].admin_enabled = false` config flag — both routes
    /// return 503 `sessions_disabled`. The UI keys off this status to
    /// render the "session storage is off" banner without inspecting
    /// the error message.
    pub sessions_disabled: bool,
    /// Phase 4 W2 4-2D: explicit data-dir override for routes that
    /// need to read per-tenant SQLite files. `None` falls back to the
    /// `CORLINMAN_DATA_DIR` env var or `~/.corlinman`. Tests pin a
    /// tempdir here to avoid the parallel-test race that the env-var
    /// fallback would have if two tests set it concurrently.
    pub data_dir: Option<PathBuf>,
    /// Phase 4 W2 B2 iter 6: per-tenant identity store backing
    /// `/admin/identity*`. `None` is the disabled gate — every
    /// `/admin/identity*` route then returns 503
    /// `identity_disabled`, mirroring the sessions/tenants
    /// disabled-503 convention. Boots that opt-in install a
    /// `SqliteIdentityStore` opened against the tenant's
    /// `user_identity.sqlite`; tests build one over a tempdir.
    pub identity_store: Option<Arc<dyn IdentityStore>>,
}

impl AdminState {
    pub fn new(plugins: Arc<PluginRegistry>, config: Arc<ArcSwap<Config>>) -> Self {
        Self {
            plugins,
            config,
            approval_gate: None,
            session_store: None,
            config_path: None,
            log_broadcast: None,
            rag_store: None,
            scheduler_history: None,
            py_config_path: None,
            config_watcher: None,
            evolution_store: None,
            evolution_applier: None,
            history_repo: None,
            proposals_repo: None,
            tenant_pool: None,
            allowed_tenants: BTreeSet::new(),
            admin_db: None,
            sessions_disabled: false,
            data_dir: None,
            identity_store: None,
        }
    }

    /// Fluent: attach the approval gate so `/admin/approvals*` routes
    /// can read the SQLite queue and wake parked decisions.
    pub fn with_approval_gate(mut self, gate: Arc<ApprovalGate>) -> Self {
        self.approval_gate = Some(gate);
        self
    }

    /// Fluent: attach the session store so `/admin/login` can issue
    /// cookies and `require_admin` can validate them.
    pub fn with_session_store(mut self, store: Arc<AdminSessionStore>) -> Self {
        self.session_store = Some(store);
        self
    }

    /// Fluent: attach the on-disk config path so `POST /admin/config`
    /// can persist accepted payloads back to the same file the loader
    /// read at boot.
    pub fn with_config_path(mut self, path: PathBuf) -> Self {
        self.config_path = Some(path);
        self
    }

    /// Fluent: attach the tracing broadcast sender so
    /// `/admin/logs/stream` can subscribe new receivers.
    pub fn with_log_broadcast(mut self, tx: broadcast::Sender<LogRecord>) -> Self {
        self.log_broadcast = Some(tx);
        self
    }

    /// Fluent: attach the RAG SQLite store so `/admin/rag/*` routes can
    /// read stats, run BM25 debug queries, and rebuild the FTS index.
    pub fn with_rag_store(mut self, store: Arc<SqliteStore>) -> Self {
        self.rag_store = Some(store);
        self
    }

    /// Fluent: attach the scheduler history buffer so
    /// `/admin/scheduler/history` has a non-empty source of truth.
    pub fn with_scheduler_history(mut self, history: Arc<scheduler::SchedulerHistory>) -> Self {
        self.scheduler_history = Some(history);
        self
    }

    /// Fluent: attach the path of the Python-side JSON config drop so
    /// admin write handlers can re-serialise after every mutation.
    pub fn with_py_config_path(mut self, path: PathBuf) -> Self {
        self.py_config_path = Some(path);
        self
    }

    /// Fluent: attach the live `ConfigWatcher` so `/admin/config/reload`
    /// can trigger a manual hot-reload.
    pub fn with_config_watcher(mut self, watcher: Arc<ConfigWatcher>) -> Self {
        self.config_watcher = Some(watcher);
        self
    }

    /// Fluent: attach the shared `EvolutionStore` so `/admin/evolution/*`
    /// has a real backing database. The same SQLite the observer writes
    /// signals into — sharing the store keeps the EvolutionLoop to one
    /// file on disk and one connection budget.
    pub fn with_evolution_store(mut self, store: Arc<EvolutionStore>) -> Self {
        self.evolution_store = Some(store);
        self
    }

    /// Fluent: attach the live `EvolutionApplier`. When set,
    /// `POST /admin/evolution/:id/apply` runs the real `memory_op`
    /// pipeline (kb mutation + history insert + proposal flip). Absent
    /// → the route 503s alongside the rest of the evolution surface.
    pub fn with_evolution_applier(mut self, applier: Arc<EvolutionApplier>) -> Self {
        self.evolution_applier = Some(applier);
        self
    }

    /// Phase 3.1 fluent: attach `HistoryRepo` + `ProposalsRepo` so
    /// `/admin/memory/decay/reset` can record its forward-correction in
    /// `evolution_history`. Both share the same `evolution.sqlite` pool
    /// the observer writes signals into, so passing them together keeps
    /// the connection budget unchanged.
    pub fn with_history_repo(
        mut self,
        history_repo: HistoryRepo,
        proposals_repo: ProposalsRepo,
    ) -> Self {
        self.history_repo = Some(history_repo);
        self.proposals_repo = Some(proposals_repo);
        self
    }

    /// Phase 4 W1 4-1A fluent: attach the multi-tenant SQLite pool. Only
    /// set this when `[tenants].enabled = true` — leaving it `None`
    /// keeps the gateway in legacy single-tenant mode where every
    /// request resolves to `TenantId::legacy_default()`.
    pub fn with_tenant_pool(mut self, pool: Arc<TenantPool>) -> Self {
        self.tenant_pool = Some(pool);
        self
    }

    /// Phase 4 W1 4-1A fluent: install the operator-allowed tenant set
    /// the tenant-scoping middleware uses to authorise session claims
    /// and `?tenant=` queries. Replaces (not extends) the existing set;
    /// callers compose the union of `[tenants].allowed` + `tenants.sqlite`
    /// rows themselves at boot.
    pub fn with_allowed_tenants(mut self, allowed: BTreeSet<TenantId>) -> Self {
        self.allowed_tenants = allowed;
        self
    }

    /// Phase 4 W1 4-1B fluent: attach the `tenants.sqlite` admin DB so
    /// `/admin/tenants*` routes have a real backing store. Boot code
    /// only calls this after a successful `AdminDb::open` — leaving it
    /// `None` is the operator-facing 503 path (config says multi-tenant
    /// is on but we couldn't open the file).
    pub fn with_admin_db(mut self, db: Arc<AdminDb>) -> Self {
        self.admin_db = Some(db);
        self
    }

    /// Phase 4 W2 4-2D fluent: flip the sessions admin surface off.
    /// All `/admin/sessions*` routes then return 503 `sessions_disabled`
    /// matching the UI mock contract. Tests use this to exercise the
    /// banner path without touching real session files.
    pub fn with_sessions_disabled(mut self, disabled: bool) -> Self {
        self.sessions_disabled = disabled;
        self
    }

    /// Phase 4 W2 4-2D fluent: pin the data dir handlers should read
    /// per-tenant SQLite files from. Production boot leaves this `None`
    /// so the env-var fallback applies; tests pin a tempdir here to
    /// dodge the global-env race.
    pub fn with_data_dir(mut self, dir: PathBuf) -> Self {
        self.data_dir = Some(dir);
        self
    }

    /// Phase 4 W2 B2 iter 6 fluent: attach the per-tenant
    /// [`IdentityStore`] backing `/admin/identity*`. Boots that opt
    /// in pass a `SqliteIdentityStore` here. Test harnesses pin a
    /// tempdir-backed store; absence of the field is the route-side
    /// 503 `identity_disabled` gate.
    pub fn with_identity_store(mut self, store: Arc<dyn IdentityStore>) -> Self {
        self.identity_store = Some(store);
        self
    }

    /// Re-serialise the current config snapshot to the Python-side JSON
    /// drop. No-op + warn when the path isn't configured — admin writes
    /// still succeed (the TOML write already landed), they just can't
    /// propagate to the Python side until the next process restart.
    pub async fn rewrite_py_config(&self) {
        let Some(path) = self.py_config_path.as_ref() else {
            return;
        };
        let cfg = self.config.load_full();
        if let Err(err) = crate::py_config::write_py_config(&cfg, path).await {
            tracing::warn!(
                error = %err,
                path = %path.display(),
                "py-config: rewrite after admin mutation failed",
            );
        }
    }
}

/// Legacy stub — kept so `routes::router()` compiles before a state-bearing
/// caller is wired up. Returns 501 for every `/admin/*` request.
pub fn router() -> Router {
    Router::new().route("/admin/*path", any(|| not_implemented("/admin/*")))
}

/// Production admin router: real handlers + auth guard (cookie first,
/// Basic-auth fallback). Login/logout/me routes are merged *outside* the
/// guard so unauthenticated callers can obtain a session.
///
/// Phase 4 W1 4-1A Item 3: a tenant-scoping layer sits *inside*
/// `require_admin` (so anonymous callers see 401 before any tenant
/// check) and *outside* the per-route handlers (so handlers always
/// observe a resolved `TenantId` in axum extensions). The layer is a
/// no-op when `[tenants].enabled = false`, in which case every
/// request resolves to `TenantId::legacy_default()` — preserving the
/// pre-Phase-4 behaviour.
pub fn router_with_state(state: AdminState) -> Router {
    let mut auth_state = AdminAuthState::new(state.config.clone());
    if let Some(store) = state.session_store.as_ref() {
        auth_state = auth_state.with_session_store(store.clone());
    }

    let cfg_snap = state.config.load();
    let tenant_state = TenantScopeState {
        enabled: cfg_snap.tenants.enabled,
        allowed: Arc::new(state.allowed_tenants.clone()),
        fallback: TenantId::new(&cfg_snap.tenants.default).unwrap_or_else(|_| {
            tracing::warn!(
                slug = %cfg_snap.tenants.default,
                "[tenants].default rejected by TenantId::new; falling back to reserved 'default'",
            );
            TenantId::legacy_default()
        }),
    };

    let guarded = Router::new()
        .merge(plugins::router(state.clone()))
        .merge(agents::router(state.clone()))
        .merge(approvals::router(state.clone()))
        .merge(config::router(state.clone()))
        .merge(logs::router(state.clone()))
        .merge(models::router(state.clone()))
        .merge(providers::router(state.clone()))
        .merge(embedding::router(state.clone()))
        .merge(rag::router(state.clone()))
        .merge(channels::router(state.clone()))
        .merge(scheduler::router(state.clone()))
        .merge(evolution::router(state.clone()))
        .merge(memory::router(state.clone()))
        .merge(sessions::router(state.clone()))
        .merge(identity::router(state.clone()))
        .merge(tenants::router(state.clone()))
        .merge(federation::router(state.clone()))
        .layer(axum::middleware::from_fn_with_state(
            tenant_state,
            tenant_scope,
        ))
        .layer(axum::middleware::from_fn_with_state(
            auth_state,
            require_admin,
        ));

    // `/admin/login`, `/admin/logout`, `/admin/me` — outside the guard.
    // Each handler does its own credential check (argon2 verify or
    // cookie validate) to avoid the chicken-and-egg problem.
    guarded.merge(auth::router(state))
}

#[cfg(test)]
mod tests {
    use super::*;
    use argon2::password_hash::{PasswordHasher, SaltString};
    use argon2::Argon2;
    use axum::body::Body;
    use axum::http::{header, Request, StatusCode};
    use base64::Engine;
    use corlinman_plugins::registry::PluginRegistry;
    use tower::ServiceExt;

    fn hash_password(password: &str) -> String {
        let salt = SaltString::encode_b64(b"corlinman_test_salt_bytes_16").unwrap();
        Argon2::default()
            .hash_password(password.as_bytes(), &salt)
            .unwrap()
            .to_string()
    }

    fn test_app() -> Router {
        let mut cfg = Config::default();
        cfg.admin.username = Some("admin".into());
        cfg.admin.password_hash = Some(hash_password("secret"));
        let state = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        );
        router_with_state(state)
    }

    fn basic(u: &str, p: &str) -> String {
        format!(
            "Basic {}",
            base64::engine::general_purpose::STANDARD.encode(format!("{u}:{p}"))
        )
    }

    #[tokio::test]
    async fn admin_routes_require_auth() {
        let resp = test_app()
            .oneshot(
                Request::builder()
                    .uri("/admin/plugins")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn admin_routes_accept_valid_basic_auth() {
        let resp = test_app()
            .oneshot(
                Request::builder()
                    .uri("/admin/plugins")
                    .header(header::AUTHORIZATION, basic("admin", "secret"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }
}
