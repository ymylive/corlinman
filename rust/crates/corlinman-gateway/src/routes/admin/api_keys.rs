//! `/admin/api_keys*` — operator-facing API-key mint surface
//! (Phase 4 W3 C4 iter 2).
//!
//! The Swift macOS reference client (`apps/swift-mac/`) and any other
//! native client needs a per-(user, tenant) bearer token to call
//! `/v1/chat/completions`. Today the gateway has no such mint endpoint —
//! tokens are only ever issued at boot via static config. This module
//! plugs the gap: an operator authenticated for the current tenant
//! (via the existing admin auth + tenant-scoping layers) can `POST`
//! to mint a fresh bearer token, `GET` to list active keys, and
//! `DELETE` to revoke.
//!
//! Three routes, all behind `require_admin` and `tenant_scope`:
//!
//! - `POST   /admin/api_keys`
//!   body  `{ scope: string, username?: string, label?: string }`
//!   → 201 `{ key_id, token, scope, username, label, tenant_id,
//!   created_at_ms }`. The cleartext `token` is returned
//!   **once** — subsequent listings show the hash only.
//! - `GET    /admin/api_keys`
//!   → `{ keys: [{ key_id, scope, username, label, created_at_ms,
//!   last_used_at_ms }] }`. Active keys for the
//!   resolved tenant, ordered by `created_at_ms DESC`.
//! - `DELETE /admin/api_keys/:key_id`
//!   → `{ revoked: bool }`. `false` is a 404 (key already revoked or
//!   never existed) so the UI can distinguish typos from idempotent
//!   revokes.
//!
//! ### Disabled / not-found paths
//!
//! - **503 `tenants_disabled`** when `AdminState::admin_db` is `None`
//!   (either `[tenants].enabled = false` or the boot-time `AdminDb::open`
//!   failed). Same envelope `/admin/tenants*` and `/admin/federation*`
//!   already return; the UI keys off the 503 status to render the
//!   "multi-tenant is off" banner.
//! - **400 `invalid_request`** when the body's `scope` is empty.
//! - **404 `not_found`** on `DELETE` when no row was flipped.
//!
//! ### Username resolution
//!
//! The body's optional `username` field defaults to `"admin"` when
//! absent. Future iterations will resolve the operator's username from
//! the admin session extension (mirrors the `accepted_by` resolution in
//! `routes/admin/federation.rs`).
//!
//! ### Cleartext-once contract
//!
//! `POST` is the only path that ever returns the cleartext `token`.
//! The token is a 67-char string (`ck_` prefix + 64 hex chars from
//! two concatenated `uuid::Uuid::new_v4().simple()` blobs). The
//! sha256 hash is stored; the cleartext is dropped from server memory
//! as soon as the JSON body is serialised. Operators who lose a token
//! must mint a fresh one — there is **no** server-side recovery.

use std::sync::Arc;

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{delete, get},
    Json, Router,
};
use corlinman_tenant::{AdminDb, ApiKeyRow, MintedApiKey};
use serde::{Deserialize, Serialize};
use serde_json::json;
use tracing::warn;

use super::AdminState;
use crate::middleware::tenant_scope::Tenant;

/* ------------------------------------------------------------------ */
/*                            Wire shapes                              */
/* ------------------------------------------------------------------ */

/// `POST /admin/api_keys` body. `scope` is required; `username` and
/// `label` are optional.
#[derive(Debug, Deserialize)]
pub struct MintBody {
    pub scope: String,
    #[serde(default)]
    pub username: Option<String>,
    #[serde(default)]
    pub label: Option<String>,
}

/// `POST /admin/api_keys` response. The only place `token` ever appears
/// in the wire surface — listings and revoke calls expose `key_id`
/// only.
#[derive(Debug, Serialize)]
pub struct MintResponse {
    pub key_id: String,
    pub tenant_id: String,
    pub username: String,
    pub scope: String,
    pub label: Option<String>,
    pub token: String,
    pub created_at_ms: i64,
}

impl MintResponse {
    fn from_minted(m: MintedApiKey) -> Self {
        Self {
            key_id: m.row.key_id,
            tenant_id: m.row.tenant_id.as_str().to_string(),
            username: m.row.username,
            scope: m.row.scope,
            label: m.row.label,
            token: m.token,
            created_at_ms: m.row.created_at_ms,
        }
    }
}

/// One row of `tenant_api_keys` projected onto the `GET` wire envelope.
/// Mirrors [`ApiKeyRow`] but emits `TenantId` as its bare slug and
/// **omits** `token_hash` — that field is internal-only.
#[derive(Debug, Serialize)]
pub struct ApiKeyOut {
    pub key_id: String,
    pub tenant_id: String,
    pub username: String,
    pub scope: String,
    pub label: Option<String>,
    pub created_at_ms: i64,
    pub last_used_at_ms: Option<i64>,
}

impl From<ApiKeyRow> for ApiKeyOut {
    fn from(r: ApiKeyRow) -> Self {
        Self {
            key_id: r.key_id,
            tenant_id: r.tenant_id.as_str().to_string(),
            username: r.username,
            scope: r.scope,
            label: r.label,
            created_at_ms: r.created_at_ms,
            last_used_at_ms: r.last_used_at_ms,
        }
    }
}

/* ------------------------------------------------------------------ */
/*                              Routes                                 */
/* ------------------------------------------------------------------ */

/// Sub-router for `/admin/api_keys*`. Mounted by
/// [`super::router_with_state`] alongside the rest of the admin
/// surface.
pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/admin/api_keys", get(list_keys).post(mint_key))
        .route("/admin/api_keys/:key_id", delete(revoke_key))
        .with_state(state)
}

fn tenants_disabled() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({
            "error": "tenants_disabled",
            "message": "tenant admin DB is not configured on this gateway",
        })),
    )
        .into_response()
}

fn require_admin_db(state: &AdminState) -> Result<Arc<AdminDb>, Box<Response>> {
    match state.admin_db.as_ref() {
        Some(db) => Ok(db.clone()),
        None => Err(Box::new(tenants_disabled())),
    }
}

async fn mint_key(
    State(state): State<AdminState>,
    Tenant(tenant_id): Tenant,
    Json(body): Json<MintBody>,
) -> Response {
    let db = match require_admin_db(&state) {
        Ok(db) => db,
        Err(resp) => return *resp,
    };

    let scope = body.scope.trim();
    if scope.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({
                "error": "invalid_request",
                "message": "`scope` is required and must be non-empty",
            })),
        )
            .into_response();
    }

    let username = body
        .username
        .as_deref()
        .map(str::trim)
        .filter(|u| !u.is_empty())
        .unwrap_or("admin");
    let label = body
        .label
        .as_deref()
        .map(str::trim)
        .filter(|l| !l.is_empty());

    match db.mint_api_key(&tenant_id, username, scope, label).await {
        Ok(minted) => {
            (StatusCode::CREATED, Json(MintResponse::from_minted(minted))).into_response()
        }
        Err(err) => {
            warn!(error = %err, tenant = %tenant_id.as_str(), "admin/api_keys: mint failed");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({
                    "error": "mint_failed",
                    "message": err.to_string(),
                })),
            )
                .into_response()
        }
    }
}

async fn list_keys(State(state): State<AdminState>, Tenant(tenant_id): Tenant) -> Response {
    let db = match require_admin_db(&state) {
        Ok(db) => db,
        Err(resp) => return *resp,
    };

    match db.list_api_keys(&tenant_id).await {
        Ok(rows) => {
            let keys: Vec<ApiKeyOut> = rows.into_iter().map(ApiKeyOut::from).collect();
            Json(json!({ "keys": keys })).into_response()
        }
        Err(err) => {
            warn!(error = %err, tenant = %tenant_id.as_str(), "admin/api_keys: list failed");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({
                    "error": "list_failed",
                    "message": err.to_string(),
                })),
            )
                .into_response()
        }
    }
}

async fn revoke_key(
    State(state): State<AdminState>,
    Tenant(_tenant_id): Tenant,
    Path(key_id): Path<String>,
) -> Response {
    let db = match require_admin_db(&state) {
        Ok(db) => db,
        Err(resp) => return *resp,
    };

    match db.revoke_api_key(&key_id).await {
        Ok(true) => Json(json!({
            "revoked": true,
            "key_id": key_id,
        }))
        .into_response(),
        Ok(false) => (
            StatusCode::NOT_FOUND,
            Json(json!({
                "error": "not_found",
                "resource": "api_key",
                "key_id": key_id,
            })),
        )
            .into_response(),
        Err(err) => {
            warn!(error = %err, key_id = %key_id, "admin/api_keys: revoke failed");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({
                    "error": "revoke_failed",
                    "message": err.to_string(),
                })),
            )
                .into_response()
        }
    }
}

/* ------------------------------------------------------------------ */
/*                              Tests                                  */
/* ------------------------------------------------------------------ */

#[cfg(test)]
mod tests {
    use super::*;
    use crate::middleware::admin_auth::{require_admin, AdminAuthState};
    use crate::middleware::tenant_scope::{tenant_scope, TenantScopeState};
    use arc_swap::ArcSwap;
    use argon2::password_hash::{PasswordHasher, SaltString};
    use argon2::Argon2;
    use axum::body::{to_bytes, Body};
    use axum::http::{header, Request, StatusCode};
    use base64::Engine;
    use corlinman_core::config::Config;
    use corlinman_plugins::registry::PluginRegistry;
    use corlinman_tenant::{AdminDb, TenantId};
    use std::collections::BTreeSet;
    use tempfile::TempDir;
    use tower::ServiceExt;

    fn hash_password(p: &str) -> String {
        let salt = SaltString::encode_b64(b"corlinman_test_salt_bytes_16").unwrap();
        Argon2::default()
            .hash_password(p.as_bytes(), &salt)
            .unwrap()
            .to_string()
    }

    fn basic(u: &str, p: &str) -> String {
        format!(
            "Basic {}",
            base64::engine::general_purpose::STANDARD.encode(format!("{u}:{p}"))
        )
    }

    async fn fixture(enable_tenants: bool) -> (Router, AdminState, TempDir) {
        let tmp = TempDir::new().unwrap();
        let admin_db_path = tmp.path().join("tenants.sqlite");
        let admin_db = AdminDb::open(&admin_db_path).await.unwrap();

        let acme = TenantId::new("acme").unwrap();
        admin_db.create_tenant(&acme, "Acme Corp", 1).await.unwrap();

        let mut cfg = Config::default();
        cfg.admin.username = Some("admin".into());
        cfg.admin.password_hash = Some(hash_password("secret"));
        cfg.tenants.enabled = enable_tenants;
        cfg.tenants.default = "acme".into();

        let admin_db = Arc::new(admin_db);
        let mut state = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg.clone())),
        );
        if enable_tenants {
            state = state.with_admin_db(admin_db.clone());
            let mut allowed: BTreeSet<TenantId> = BTreeSet::new();
            allowed.insert(acme.clone());
            state = state.with_allowed_tenants(allowed);
        }

        // Build the router with auth + tenant_scope just like
        // `super::router_with_state` does — without the rest of the
        // admin surface so the test is bounded.
        let cfg_arc = state.config.clone();
        let auth_state = AdminAuthState::new(cfg_arc.clone());
        let tenant_state = TenantScopeState {
            enabled: enable_tenants,
            allowed: Arc::new(state.allowed_tenants.clone()),
            fallback: TenantId::new(&cfg.tenants.default).unwrap(),
        };
        let app = router(state.clone())
            .layer(axum::middleware::from_fn_with_state(
                tenant_state,
                tenant_scope,
            ))
            .layer(axum::middleware::from_fn_with_state(
                auth_state,
                require_admin,
            ));

        (app, state, tmp)
    }

    fn auth_req(method: &str, uri: &str, body: Body) -> Request<Body> {
        Request::builder()
            .method(method)
            .uri(uri)
            .header(header::AUTHORIZATION, basic("admin", "secret"))
            .header(header::CONTENT_TYPE, "application/json")
            .body(body)
            .unwrap()
    }

    #[tokio::test]
    async fn mint_returns_cleartext_once_then_lists_without_it() {
        let (app, _state, _tmp) = fixture(true).await;

        // Mint.
        let body = serde_json::to_vec(&serde_json::json!({
            "scope": "chat",
            "label": "MacBook"
        }))
        .unwrap();
        let resp = app
            .clone()
            .oneshot(auth_req(
                "POST",
                "/admin/api_keys?tenant=acme",
                Body::from(body),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::CREATED);
        let bytes = to_bytes(resp.into_body(), 64 * 1024).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        let token = v["token"]
            .as_str()
            .expect("token must appear in mint response");
        assert!(token.starts_with("ck_"));
        assert_eq!(token.len(), 67);
        assert_eq!(v["tenant_id"], "acme");
        assert_eq!(v["scope"], "chat");
        assert_eq!(v["label"], "MacBook");
        assert_eq!(v["username"], "admin");
        let key_id = v["key_id"].as_str().unwrap().to_string();

        // List — token must NOT appear; the row's metadata does.
        let resp = app
            .clone()
            .oneshot(auth_req(
                "GET",
                "/admin/api_keys?tenant=acme",
                Body::empty(),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = to_bytes(resp.into_body(), 64 * 1024).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        let keys = v["keys"].as_array().expect("keys array");
        assert_eq!(keys.len(), 1);
        assert_eq!(keys[0]["key_id"], key_id);
        assert_eq!(keys[0]["scope"], "chat");
        assert_eq!(keys[0]["label"], "MacBook");
        assert!(
            keys[0].get("token").is_none(),
            "list must not leak the cleartext"
        );
        assert!(
            keys[0].get("token_hash").is_none(),
            "list must not leak the hash either"
        );
    }

    #[tokio::test]
    async fn mint_rejects_empty_scope() {
        let (app, _state, _tmp) = fixture(true).await;
        let body = serde_json::to_vec(&serde_json::json!({ "scope": "  " })).unwrap();
        let resp = app
            .oneshot(auth_req(
                "POST",
                "/admin/api_keys?tenant=acme",
                Body::from(body),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let bytes = to_bytes(resp.into_body(), 4 * 1024).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["error"], "invalid_request");
    }

    #[tokio::test]
    async fn revoke_then_list_excludes_revoked_key() {
        let (app, _state, _tmp) = fixture(true).await;

        // Mint two keys; revoke one.
        let mut key_ids: Vec<String> = Vec::new();
        for label in ["alpha", "bravo"] {
            let body = serde_json::to_vec(&serde_json::json!({
                "scope": "chat",
                "label": label,
            }))
            .unwrap();
            let resp = app
                .clone()
                .oneshot(auth_req(
                    "POST",
                    "/admin/api_keys?tenant=acme",
                    Body::from(body),
                ))
                .await
                .unwrap();
            assert_eq!(resp.status(), StatusCode::CREATED);
            let bytes = to_bytes(resp.into_body(), 64 * 1024).await.unwrap();
            let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
            key_ids.push(v["key_id"].as_str().unwrap().to_string());
        }

        let revoke_uri = format!("/admin/api_keys/{}?tenant=acme", key_ids[0]);
        let resp = app
            .clone()
            .oneshot(auth_req("DELETE", &revoke_uri, Body::empty()))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = to_bytes(resp.into_body(), 4 * 1024).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["revoked"], true);

        // Re-revoke the same key → 404 not_found (idempotent miss).
        let resp = app
            .clone()
            .oneshot(auth_req("DELETE", &revoke_uri, Body::empty()))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);

        // List has only the survivor.
        let resp = app
            .clone()
            .oneshot(auth_req(
                "GET",
                "/admin/api_keys?tenant=acme",
                Body::empty(),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = to_bytes(resp.into_body(), 64 * 1024).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        let keys = v["keys"].as_array().unwrap();
        assert_eq!(keys.len(), 1);
        assert_eq!(keys[0]["key_id"], key_ids[1]);
    }

    #[tokio::test]
    async fn requires_admin_auth() {
        let (app, _state, _tmp) = fixture(true).await;
        // No Authorization header — middleware short-circuits with 401.
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/api_keys?tenant=acme")
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(r#"{"scope":"chat"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn missing_admin_db_returns_503() {
        // Tenants disabled → AdminState has no `admin_db` → 503
        // tenants_disabled.
        let tmp = TempDir::new().unwrap();
        let _ = tmp; // silence unused-var without dropping the dir

        let mut cfg = Config::default();
        cfg.admin.username = Some("admin".into());
        cfg.admin.password_hash = Some(hash_password("secret"));
        cfg.tenants.enabled = false;
        cfg.tenants.default = "default".into();

        let state = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg.clone())),
        );

        let auth_state = AdminAuthState::new(state.config.clone());
        let tenant_state = TenantScopeState {
            enabled: false,
            allowed: Arc::new(BTreeSet::new()),
            fallback: TenantId::legacy_default(),
        };
        let app = router(state)
            .layer(axum::middleware::from_fn_with_state(
                tenant_state,
                tenant_scope,
            ))
            .layer(axum::middleware::from_fn_with_state(
                auth_state,
                require_admin,
            ));

        let resp = app
            .oneshot(auth_req(
                "POST",
                "/admin/api_keys",
                Body::from(r#"{"scope":"chat"}"#),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
        let bytes = to_bytes(resp.into_body(), 4 * 1024).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["error"], "tenants_disabled");
    }
}
