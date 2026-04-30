//! `/admin/tenants*` — operator-only multi-tenant registry routes.
//!
//! Phase 4 W1 4-1B Item 4. Two routes, both behind the existing
//! `require_admin` cookie/Basic-auth guard and inside the `tenant_scope`
//! middleware. These are tenant-management routes (they create new
//! tenants and list the roster), so they don't consult the resolved
//! `?tenant=` extension — the resolved tenant only matters for routes
//! that read or write *per-tenant* data.
//!
//! - `GET  /admin/tenants` — list active rows from `tenants.sqlite`
//!   plus the operator-allowed set already on `AdminState`. Both fields
//!   are emitted from one fetch so the UI's switcher and table render
//!   from a single round trip.
//! - `POST /admin/tenants` — translation of `corlinman tenant create`
//!   into an HTTP route: validate slug, create the per-tenant data dir
//!   under `<data_dir>/tenants/<slug>/`, insert the tenant row +
//!   argon2id-hashed admin row in `tenants.sqlite`. Mirrors the CLI's
//!   `run_create` flow byte-for-byte; only the input source differs.
//!
//! ### Disabled / unconfigured paths
//!
//! - **403 `tenants_disabled`** when `[tenants].enabled = false`. The
//!   UI uses this to render the "multi-tenant mode is off" banner.
//! - **503 `tenants_disabled` + `reason=admin_db_missing`** when the
//!   gateway booted with `[tenants].enabled = true` but failed to open
//!   `tenants.sqlite` (e.g. a read-only data dir). Operator-visible
//!   error rather than a silent 500 so misconfigurations surface
//!   loudly.
//!
//! ### Argon2 helper duplication
//!
//! `hash_password` is a 4-line wrapper around the same
//! `argon2::Argon2::default().hash_password(...)` call the CLI uses in
//! `corlinman-cli::cmd::tenant::hash_password`. Duplicated inline here
//! rather than extracted into `corlinman-tenant` because the
//! `corlinman-tenant` API surface is already pinned by Phase 4 W1
//! 4-1A and adding a public helper would widen it for a single
//! caller. Both copies must agree on `argon2::Argon2::default()` —
//! the salt is per-call random; format is the standard
//! `$argon2id$v=19$...` PHC string.

use std::path::PathBuf;
use std::sync::Arc;

use argon2::password_hash::{rand_core::OsRng, PasswordHasher, SaltString};
use argon2::Argon2;
use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::get,
    Json, Router,
};
use corlinman_tenant::{tenant_root_dir, AdminDb, AdminDbError, TenantId};
use serde::{Deserialize, Serialize};
use serde_json::json;
use tracing::warn;

use super::AdminState;

/// Output row shape for `GET /admin/tenants`. Mirrors the UI's
/// `TenantRow` interface in `ui/lib/api/tenants.ts`. `created_at` is
/// emitted as an RFC-3339 / ISO-8601 string, not the raw unix-millis
/// the SQLite column carries — the UI displays it via `Date()` and we
/// don't want every consumer doing the millis-to-string conversion.
#[derive(Debug, Serialize)]
pub struct TenantOut {
    pub tenant_id: String,
    pub display_name: String,
    pub created_at: String,
}

/// Wire shape for `GET /admin/tenants`. Mirrors the UI's
/// `TenantsListResponse` interface.
#[derive(Debug, Serialize)]
pub struct TenantsListOut {
    pub tenants: Vec<TenantOut>,
    pub allowed: Vec<String>,
}

/// Body for `POST /admin/tenants`. Mirrors the UI's
/// `TenantCreateBody`. `display_name` is optional; when omitted the
/// slug doubles as the display name (matches the CLI default).
#[derive(Debug, Deserialize)]
pub struct CreateBody {
    pub slug: String,
    #[serde(default)]
    pub display_name: Option<String>,
    pub admin_username: String,
    pub admin_password: String,
}

/// Wire shape for `POST /admin/tenants` 201 response. Mirrors the UI's
/// `TenantCreateResponse`.
#[derive(Debug, Serialize)]
pub struct CreateOut {
    pub tenant_id: String,
}

/// Sub-router for `/admin/tenants*`. Mounted by
/// [`super::router_with_state`] inside both `require_admin` and
/// `tenant_scope`.
pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/admin/tenants", get(list_tenants).post(create_tenant))
        // Phase 4 W1.5 (next-tasks A4): per-tenant content reads for
        // the operator UI's diff view. Both kinds whose proposers
        // emit `diff.before = ""` (the applier reads live state at
        // apply time) need a way to render the on-disk content
        // alongside the proposed `diff.after`. Single GET endpoint
        // per kind keeps the contract narrow; future kinds add
        // sibling routes.
        .route(
            "/admin/tenants/:tenant/prompt_segments/:name",
            get(read_prompt_segment),
        )
        .route(
            "/admin/tenants/:tenant/agent_cards/:name",
            get(read_agent_card),
        )
        .with_state(state)
}

/// Wire shape for `GET /admin/tenants/:tenant/{prompt_segments,agent_cards}/:name`.
///
/// `exists = false` is a legitimate response shape (not a 404):
/// `prompt_template` and `agent_card` proposals can apply against a
/// segment that doesn't exist yet (operator is creating the segment
/// from scratch). The UI's diff view should render `before = ""` in
/// that case rather than a "not found" error.
#[derive(Debug, Serialize)]
pub struct TenantContentOut {
    pub tenant_id: String,
    pub kind: String,
    pub name: String,
    pub exists: bool,
    pub content: String,
}

/// Convert a unix-millis timestamp from `tenants.sqlite` into an
/// RFC-3339 / ISO-8601 string. Falls back to the millis as a stringy
/// number when the timestamp can't be represented (well outside any
/// sane range).
fn format_created_at_ms(ms: i64) -> String {
    let nanos = (ms as i128) * 1_000_000;
    match time::OffsetDateTime::from_unix_timestamp_nanos(nanos) {
        Ok(dt) => dt
            .format(&time::format_description::well_known::Rfc3339)
            .unwrap_or_else(|_| ms.to_string()),
        Err(_) => ms.to_string(),
    }
}

fn now_unix_ms() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

/// Phase 4 W1.5 (next-tasks A4): minimal defensive validation on a
/// `prompt_segment` id read from the URL path. The full validator
/// lives in `evolution_applier::validate_prompt_segment_id` (the
/// source of truth for the write path); this is a slimmer copy
/// that keeps rejected names out of the filesystem read. Drift
/// between the two is OK: the applier-side validator is stricter
/// (size cap, control-char ban) — anything this fn passes the
/// applier rejects on apply, so the route stays defensive without
/// being a duplicate spec.
fn is_valid_segment_name(s: &str) -> bool {
    if s.is_empty() || s.len() > 128 {
        return false;
    }
    if s.starts_with('.') || s.ends_with('.') || s.contains("..") {
        return false;
    }
    s.chars()
        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '.')
}

/// Phase 4 W1.5 (next-tasks A4): same shape as
/// [`is_valid_segment_name`] for agent ids, mirroring the applier's
/// `validate_agent_id` whitelist (`[a-z][a-z0-9_-]{0,63}`).
fn is_valid_agent_name(s: &str) -> bool {
    if s.is_empty() || s.len() > 64 {
        return false;
    }
    if !s
        .chars()
        .next()
        .map(|c| c.is_ascii_lowercase())
        .unwrap_or(false)
    {
        return false;
    }
    s.chars()
        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-')
}

/// Mirror of `corlinman-cli::cmd::tenant::hash_password`. Kept inline
/// rather than imported so this crate doesn't take a new dep on the
/// CLI binary crate, and `corlinman-tenant`'s API stays unchanged.
fn hash_password(password: &str) -> Result<String, argon2::password_hash::Error> {
    let salt = SaltString::generate(&mut OsRng);
    Ok(Argon2::default()
        .hash_password(password.as_bytes(), &salt)?
        .to_string())
}

/// Resolve the data directory the same way `server::resolve_data_dir`
/// does. Duplicated rather than re-exported because making the
/// server-internal helper public for one caller would widen the
/// gateway's API surface; the rule is small and stable.
fn resolve_data_dir() -> PathBuf {
    if let Ok(dir) = std::env::var("CORLINMAN_DATA_DIR") {
        return PathBuf::from(dir);
    }
    dirs::home_dir()
        .map(|h| h.join(".corlinman"))
        .unwrap_or_else(|| PathBuf::from(".corlinman"))
}

fn tenants_disabled_403() -> Response {
    (
        StatusCode::FORBIDDEN,
        Json(json!({
            "error": "tenants_disabled",
        })),
    )
        .into_response()
}

fn admin_db_missing_503() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({
            "error": "tenants_disabled",
            "reason": "admin_db_missing",
        })),
    )
        .into_response()
}

fn invalid_tenant_slug(reason: impl Into<String>) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({
            "error": "invalid_tenant_slug",
            "reason": reason.into(),
        })),
    )
        .into_response()
}

fn missing_admin_username() -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({
            "error": "missing_admin_username",
        })),
    )
        .into_response()
}

fn missing_admin_password() -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({
            "error": "missing_admin_password",
        })),
    )
        .into_response()
}

fn tenant_exists() -> Response {
    (
        StatusCode::CONFLICT,
        Json(json!({
            "error": "tenant_exists",
        })),
    )
        .into_response()
}

fn storage_error(err: impl std::fmt::Display, ctx: &'static str) -> Response {
    warn!(error = %err, "admin/tenants {ctx} failed");
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        Json(json!({
            "error": "storage_error",
            "message": err.to_string(),
        })),
    )
        .into_response()
}

/// Decide which "disabled / unconfigured" envelope applies to the
/// current request. Returns `None` when the route should proceed
/// (i.e. tenants are enabled and the admin DB is open). Returns
/// `Some(response)` otherwise.
fn disabled_envelope(state: &AdminState) -> Option<Response> {
    let cfg = state.config.load();
    if !cfg.tenants.enabled {
        return Some(tenants_disabled_403());
    }
    if state.admin_db.is_none() {
        return Some(admin_db_missing_503());
    }
    None
}

/// Read a per-tenant content file (a `prompt_template` segment or an
/// `agent_card` markdown file) and return its current bytes for the
/// operator UI's diff view. `kind` parameterises the directory layout
/// — both kinds share the same path shape and validation discipline,
/// only the directory name differs.
async fn read_tenant_content(
    state: &AdminState,
    tenant_raw: &str,
    name: &str,
    kind: &'static str,
    name_validator: fn(&str) -> bool,
    subdir: &'static str,
) -> Response {
    if let Some(resp) = disabled_envelope(state) {
        return resp;
    }

    // Validate tenant slug via the canonical `TenantId::new` (same
    // regex the middleware enforces). Reject before touching the
    // filesystem so a traversal attempt never reaches `Path::join`.
    let tenant = match TenantId::new(tenant_raw.to_string()) {
        Ok(t) => t,
        Err(err) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({
                    "error": "invalid_tenant_slug",
                    "slug": tenant_raw,
                    "reason": err.to_string(),
                })),
            )
                .into_response();
        }
    };

    // Validate the segment / agent name using the kind-specific
    // whitelist. Defense-in-depth — the applier-side validator is
    // the source of truth for the write path.
    if !name_validator(name) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({
                "error": "invalid_name",
                "kind": kind,
                "name": name,
            })),
        )
            .into_response();
    }

    // Resolve `<data_dir>/tenants/<tenant>/<subdir>/<name>.md`. The
    // path is fully derived from validated inputs, so traversal is
    // already prevented.
    let data_dir = resolve_data_dir();
    let path = tenant_root_dir(&data_dir, &tenant)
        .join(subdir)
        .join(format!("{name}.md"));

    let (exists, content) = match tokio::fs::read_to_string(&path).await {
        Ok(s) => (true, s),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => (false, String::new()),
        Err(e) => {
            warn!(
                tenant = tenant.as_str(),
                kind,
                name,
                path = %path.display(),
                error = %e,
                "admin/tenants content read failed",
            );
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({
                    "error": "storage_error",
                    "message": e.to_string(),
                })),
            )
                .into_response();
        }
    };

    Json(TenantContentOut {
        tenant_id: tenant.as_str().to_string(),
        kind: kind.to_string(),
        name: name.to_string(),
        exists,
        content,
    })
    .into_response()
}

async fn read_prompt_segment(
    State(state): State<AdminState>,
    Path((tenant, name)): Path<(String, String)>,
) -> Response {
    read_tenant_content(
        &state,
        &tenant,
        &name,
        "prompt_template",
        is_valid_segment_name,
        "prompt_segments",
    )
    .await
}

async fn read_agent_card(
    State(state): State<AdminState>,
    Path((tenant, name)): Path<(String, String)>,
) -> Response {
    read_tenant_content(
        &state,
        &tenant,
        &name,
        "agent_card",
        is_valid_agent_name,
        "agent_cards",
    )
    .await
}

async fn list_tenants(State(state): State<AdminState>) -> Response {
    if let Some(resp) = disabled_envelope(&state) {
        return resp;
    }
    // Safe: `disabled_envelope` returned None ⇒ admin_db is Some.
    let db = state.admin_db.as_ref().expect("admin_db checked above");

    let rows = match db.list_active().await {
        Ok(r) => r,
        Err(err) => return storage_error(err, "list_active"),
    };

    let tenants = rows
        .into_iter()
        .map(|r| TenantOut {
            tenant_id: r.tenant_id.into_inner(),
            display_name: r.display_name,
            created_at: format_created_at_ms(r.created_at),
        })
        .collect();

    let allowed: Vec<String> = state
        .allowed_tenants
        .iter()
        .map(|t| t.as_str().to_string())
        .collect();

    Json(TenantsListOut { tenants, allowed }).into_response()
}

async fn create_tenant(State(state): State<AdminState>, Json(body): Json<CreateBody>) -> Response {
    if let Some(resp) = disabled_envelope(&state) {
        return resp;
    }
    let db: Arc<AdminDb> = state.admin_db.clone().expect("admin_db checked above");

    // Validate slug shape via TenantId::new — same regex the CLI uses.
    let tenant_id = match TenantId::new(body.slug.clone()) {
        Ok(t) => t,
        Err(err) => return invalid_tenant_slug(err.to_string()),
    };

    // Required fields. The middleware-level json deserialiser already
    // rejected payloads missing the `admin_username` / `admin_password`
    // *keys*; we only need to reject empty strings here. UI sends both
    // unconditionally via `CreateTenantDialog` so a 400 here points at
    // a buggy client or a hand-crafted curl.
    if body.admin_username.is_empty() {
        return missing_admin_username();
    }
    if body.admin_password.is_empty() {
        return missing_admin_password();
    }

    // Per-tenant directory tree must exist before any per-tenant
    // SQLite is opened (downstream stores call `tenant_db_path(...)`
    // which assumes the parent exists).
    let data_dir = resolve_data_dir();
    if let Err(err) = std::fs::create_dir_all(&data_dir) {
        return storage_error(err, "create_data_dir");
    }
    let tenant_dir = tenant_root_dir(&data_dir, &tenant_id);
    if let Err(err) = std::fs::create_dir_all(&tenant_dir) {
        return storage_error(err, "create_tenant_dir");
    }

    let display_name = body
        .display_name
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| body.slug.clone());

    let now_ms = now_unix_ms();

    // Insert the tenant row first. A duplicate slug fails fast so we
    // don't waste an argon2 hash cycle on a request that's already
    // rejected.
    match db.create_tenant(&tenant_id, &display_name, now_ms).await {
        Ok(()) => {}
        Err(AdminDbError::TenantExists(_)) => return tenant_exists(),
        Err(err) => return storage_error(err, "create_tenant_row"),
    }

    let password_hash = match hash_password(&body.admin_password) {
        Ok(h) => h,
        Err(err) => return storage_error(err, "argon2_hash"),
    };

    if let Err(err) = db
        .add_admin(&tenant_id, &body.admin_username, &password_hash, now_ms)
        .await
    {
        // The tenant row landed but the admin row didn't; surface the
        // sqlite error rather than silently leaving a half-seeded
        // tenant. The operator can re-run the create via the CLI or
        // delete the row and retry. Soft-delete / cleanup machinery is
        // out of scope for this Wave 1 patch.
        return storage_error(err, "add_admin_row");
    }

    (
        StatusCode::CREATED,
        Json(CreateOut {
            tenant_id: tenant_id.into_inner(),
        }),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    //! Inline unit tests cover the no-fixture-needed disabled paths.
    //! The full happy-path / error-matrix exercise lives in
    //! `tests/tenant_admin_routes.rs` because it needs a tempdir and
    //! an env-var-guarded `CORLINMAN_DATA_DIR` setup that's noisier
    //! than what fits inline.
    use super::*;
    use arc_swap::ArcSwap;
    use axum::body::{to_bytes, Body};
    use axum::http::Request;
    use corlinman_core::config::Config;
    use corlinman_plugins::registry::PluginRegistry;
    use tower::ServiceExt;

    async fn body_json(res: axum::response::Response) -> serde_json::Value {
        let bytes = to_bytes(res.into_body(), usize::MAX).await.unwrap();
        serde_json::from_slice(&bytes).unwrap()
    }

    #[tokio::test]
    async fn list_returns_403_when_tenants_disabled() {
        let cfg = Config::default(); // enabled = false by default
        let state = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        );
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/tenants")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::FORBIDDEN);
        let body = body_json(resp).await;
        assert_eq!(body["error"], "tenants_disabled");
    }

    #[tokio::test]
    async fn list_returns_503_when_admin_db_missing() {
        let mut cfg = Config::default();
        cfg.tenants.enabled = true;
        // No `with_admin_db(...)` — the boot-time open failed.
        let state = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        );
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/tenants")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
        let body = body_json(resp).await;
        assert_eq!(body["error"], "tenants_disabled");
        assert_eq!(body["reason"], "admin_db_missing");
    }
}
