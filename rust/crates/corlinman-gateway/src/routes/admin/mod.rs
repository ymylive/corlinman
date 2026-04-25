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

use std::path::PathBuf;
use std::sync::Arc;

use arc_swap::ArcSwap;
use axum::{routing::any, Router};
use corlinman_core::config::Config;
use corlinman_evolution::EvolutionStore;
use corlinman_plugins::registry::PluginRegistry;
use corlinman_vector::SqliteStore;
use tokio::sync::broadcast;

use crate::config_watcher::ConfigWatcher;
use crate::log_broadcast::LogRecord;
use crate::middleware::admin_auth::{require_admin, AdminAuthState};
use crate::middleware::admin_session::AdminSessionStore;
use crate::middleware::approval::ApprovalGate;

use super::not_implemented;

pub mod agents;
pub mod approvals;
pub mod auth;
pub mod channels;
pub mod config;
pub mod embedding;
pub mod evolution;
pub mod logs;
pub mod models;
pub mod napcat;
pub mod plugins;
pub mod providers;
pub mod rag;
pub mod scheduler;

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
pub fn router_with_state(state: AdminState) -> Router {
    let mut auth_state = AdminAuthState::new(state.config.clone());
    if let Some(store) = state.session_store.as_ref() {
        auth_state = auth_state.with_session_store(store.clone());
    }

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
