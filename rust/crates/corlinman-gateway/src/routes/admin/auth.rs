//! `/admin/login`, `/admin/logout`, `/admin/me` — session lifecycle.
//!
//! S5 T1 scope: stamp / clear a `corlinman_session=<uuid>` cookie so the
//! Admin UI can sign in once and stop carrying Basic credentials around.
//!
//! These three routes mount **outside** the `require_admin` middleware —
//! otherwise `/admin/login` would itself require credentials that the UI
//! hasn't issued yet, and `/admin/me` couldn't tell the UI "you are
//! unauthenticated" distinctly from "cookie expired." The guard is
//! implemented inline instead: each handler loads the current
//! `admin.username` / `password_hash` from `AdminState.config` on every
//! call, so rotating credentials in config.toml takes effect next request.
//!
//! Status codes:
//!   - `POST /admin/login` → 200 + Set-Cookie, 401 on bad creds, 503 when
//!     no admin is configured in config.
//!   - `POST /admin/logout` → 204 + Set-Cookie max-age=0 (always, even if
//!     the caller had no cookie — idempotent).
//!   - `GET /admin/me` → 200 with session info, 401 otherwise.

use std::path::Path;

use argon2::password_hash::{rand_core::OsRng, PasswordHasher, SaltString};
use argon2::Argon2;
use axum::{
    extract::State,
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime;

use super::AdminState;
use crate::middleware::admin_auth::{argon2_verify, extract_cookie, SESSION_COOKIE_NAME};

/// Minimum length operators must use when picking the admin password.
/// Picked deliberately low (8) so first-run onboarding doesn't bounce
/// reasonable passphrases — argon2id absorbs the entropy hit. Tune up
/// later if [audit] complaints land.
pub(crate) const MIN_PASSWORD_LEN: usize = 8;

/// Cookie attributes we stamp on every Set-Cookie for the session:
///   - `HttpOnly` — JS can't read it (mitigates token theft via XSS).
///   - `SameSite=Strict` — browser won't send it on cross-site requests
///     (mitigates CSRF for state-changing routes).
///   - `Path=/` — applies to all gateway routes so `/admin/*` and
///     `/v1/*` both see the cookie.
///   - No `Secure` flag — we bind to 127.0.0.1 by default and set up
///     TLS at the reverse-proxy layer; adding Secure would break dev
///     over plain http. TODO: make this configurable when we ship a
///     built-in HTTPS listener.
///
/// `Max-Age` is filled in from `AdminSessionStore::ttl()` per call so
/// bumping the TTL via config takes effect without a code change.
fn set_cookie_header(token: &str, max_age_seconds: i64) -> String {
    format!(
        "{name}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={age}",
        name = SESSION_COOKIE_NAME,
        value = token,
        age = max_age_seconds,
    )
}

fn clear_cookie_header() -> String {
    format!(
        "{name}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
        name = SESSION_COOKIE_NAME,
    )
}

/// Router for the session routes. `/admin/login`, `/admin/logout`,
/// `/admin/me`, and `/admin/onboard` mount *without* the `require_admin`
/// middleware — see the module doc. The first-run onboarding route is
/// session-less by necessity (no admin exists yet) but self-gates by
/// inspecting `cfg.admin` on every call so it's safe to live here.
pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/admin/login", post(login))
        .route("/admin/logout", post(logout))
        .route("/admin/me", get(me))
        .route("/admin/onboard", post(onboard))
        .with_state(state)
}

#[derive(Debug, Deserialize)]
pub struct LoginRequest {
    pub username: String,
    pub password: String,
}

#[derive(Debug, Serialize)]
pub struct LoginResponse {
    /// Opaque token. The cookie is the primary mechanism; the body copy
    /// is handy for non-browser clients (curl, CI).
    pub token: String,
    /// Seconds until idle-timeout eviction. Mirrors the cookie Max-Age.
    pub expires_in: i64,
}

/// Translate an `OffsetDateTime` to RFC 3339 or an empty string on format
/// failure (should never happen with valid dates).
fn iso(dt: OffsetDateTime) -> String {
    dt.format(&Rfc3339).unwrap_or_default()
}

async fn login(State(state): State<AdminState>, Json(body): Json<LoginRequest>) -> Response {
    let cfg = state.config.load();
    let Some(expected_user) = cfg.admin.username.as_deref() else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"error": "admin_not_configured"})),
        )
            .into_response();
    };
    let Some(expected_hash) = cfg.admin.password_hash.as_deref() else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"error": "admin_not_configured"})),
        )
            .into_response();
    };

    let Some(store) = state.session_store.as_ref() else {
        // Misconfiguration: router was wired without a session store. Fall
        // back to telling the caller to use Basic auth.
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"error": "session_store_missing"})),
        )
            .into_response();
    };

    if body.username != expected_user || !argon2_verify(&body.password, expected_hash) {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "invalid_credentials"})),
        )
            .into_response();
    }

    let token = store.create(body.username.clone());
    let ttl = store.ttl();
    let max_age = ttl.as_secs() as i64;

    let mut headers = HeaderMap::new();
    if let Ok(value) = set_cookie_header(&token, max_age).parse() {
        headers.insert(header::SET_COOKIE, value);
    }

    (
        StatusCode::OK,
        headers,
        Json(LoginResponse {
            token,
            expires_in: max_age,
        }),
    )
        .into_response()
}

async fn logout(State(state): State<AdminState>, headers: HeaderMap) -> Response {
    // Best-effort: if a session store is wired and the caller has a
    // cookie, kill the token. Otherwise just clear the cookie client-side.
    if let Some(store) = state.session_store.as_ref() {
        if let Some(cookie_header) = headers.get(header::COOKIE).and_then(|v| v.to_str().ok()) {
            if let Some(token) = extract_cookie(cookie_header, SESSION_COOKIE_NAME) {
                store.invalidate(&token);
            }
        }
    }

    let mut out = HeaderMap::new();
    if let Ok(value) = clear_cookie_header().parse() {
        out.insert(header::SET_COOKIE, value);
    }
    (StatusCode::NO_CONTENT, out).into_response()
}

#[derive(Debug, Serialize)]
pub struct MeResponse {
    pub user: String,
    pub created_at: String,
    pub expires_at: String,
}

async fn me(State(state): State<AdminState>, headers: HeaderMap) -> Response {
    let Some(store) = state.session_store.as_ref() else {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "unauthenticated"})),
        )
            .into_response();
    };
    let Some(cookie_header) = headers.get(header::COOKIE).and_then(|v| v.to_str().ok()) else {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "unauthenticated"})),
        )
            .into_response();
    };
    let Some(token) = extract_cookie(cookie_header, SESSION_COOKIE_NAME) else {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "unauthenticated"})),
        )
            .into_response();
    };
    let Some(session) = store.validate(&token) else {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "session_expired"})),
        )
            .into_response();
    };

    let expires_at = session.last_used + time::Duration::seconds(store.ttl().as_secs() as i64);
    Json(MeResponse {
        user: session.user,
        created_at: iso(session.created_at),
        expires_at: iso(expires_at),
    })
    .into_response()
}

/// Default idle TTL for admin sessions (24 hours). `server.rs` uses this
/// when it wires the store into `AdminState`.
pub const DEFAULT_SESSION_TTL_SECS: u64 = 86_400;

// ---------------------------------------------------------------------------
// Onboarding + password change
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct OnboardRequest {
    pub username: String,
    pub password: String,
}

/// Hash a plaintext password with argon2id + random salt. Matches the
/// scheme `argon2_verify` consumes.
pub(crate) fn hash_password(password: &str) -> Result<String, argon2::password_hash::Error> {
    let salt = SaltString::generate(&mut OsRng);
    Ok(Argon2::default()
        .hash_password(password.as_bytes(), &salt)?
        .to_string())
}

/// `POST /admin/onboard` — first-run admin bootstrap.
///
/// Only accepted when the gateway is in "onboarding mode": the active
/// `[admin]` block in config has neither a username nor a password_hash.
/// In every other state we return 409 `already_onboarded` so a
/// compromised network surface can't blow away the existing credentials.
///
/// On success we hash the password, write the `[admin]` block to
/// `config.toml`, and hot-swap the in-memory snapshot. The operator can
/// then `/admin/login` immediately — no restart required.
async fn onboard(State(state): State<AdminState>, Json(body): Json<OnboardRequest>) -> Response {
    let cfg = state.config.load_full();
    if cfg.admin.username.is_some() || cfg.admin.password_hash.is_some() {
        return (
            StatusCode::CONFLICT,
            Json(json!({
                "error": "already_onboarded",
                "message": "admin credentials are already configured; use POST /admin/password to rotate",
            })),
        )
            .into_response();
    }

    if body.username.trim().is_empty() {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({
                "error": "invalid_username",
                "message": "username must be non-empty",
            })),
        )
            .into_response();
    }
    if body.password.len() < MIN_PASSWORD_LEN {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({
                "error": "weak_password",
                "message": format!("password must be at least {MIN_PASSWORD_LEN} characters"),
            })),
        )
            .into_response();
    }

    persist_admin_credentials(&state, body.username.trim().to_string(), &body.password).await
}

/// Shared write path used by `onboard`. Hashes
/// the password, mutates the config in-place, writes the TOML
/// atomically, and hot-swaps the in-memory snapshot. Identical contract
/// to `POST /admin/config` so operators only have to remember one
/// failure mode.
async fn persist_admin_credentials(
    state: &AdminState,
    username: String,
    plaintext_password: &str,
) -> Response {
    let Some(path) = state.config_path.as_ref() else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({
                "error": "config_path_unset",
                "message": "gateway booted without a config file path",
            })),
        )
            .into_response();
    };

    let hashed = match hash_password(plaintext_password) {
        Ok(h) => h,
        Err(err) => {
            tracing::error!(error = %err, "admin/auth: argon2 hashing failed");
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "hash_failed", "message": err.to_string()})),
            )
                .into_response();
        }
    };

    let mut new_cfg = (*state.config.load_full()).clone();
    new_cfg.admin.username = Some(username);
    new_cfg.admin.password_hash = Some(hashed);

    let serialised = match toml::to_string_pretty(&new_cfg) {
        Ok(s) => s,
        Err(err) => {
            tracing::error!(error = %err, "admin/auth: serialise config failed");
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "serialise_failed", "message": err.to_string()})),
            )
                .into_response();
        }
    };
    if let Err(err) = atomic_write(path, &serialised).await {
        tracing::error!(error = %err, path = %path.display(), "admin/auth: write config failed");
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": "write_failed", "message": err.to_string()})),
        )
            .into_response();
    }

    state.config.store(std::sync::Arc::new(new_cfg));
    // Mirror to the Python config drop so the sidecar sees the new
    // identity on its next resolve.
    state.rewrite_py_config().await;

    (StatusCode::OK, Json(json!({"status": "ok"}))).into_response()
}

async fn atomic_write(path: &Path, contents: &str) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    let mut tmp = path.to_path_buf();
    tmp.as_mut_os_string().push(".new");
    tokio::fs::write(&tmp, contents).await?;
    tokio::fs::rename(&tmp, path).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::middleware::admin_session::AdminSessionStore;
    use arc_swap::ArcSwap;
    use argon2::password_hash::{PasswordHasher, SaltString};
    use argon2::Argon2;
    use axum::body::{to_bytes, Body};
    use axum::http::{header, Request, StatusCode};
    use corlinman_core::config::Config;
    use corlinman_plugins::registry::PluginRegistry;
    use std::sync::Arc;
    use std::time::Duration as StdDuration;
    use tower::ServiceExt;

    fn hash_password(password: &str) -> String {
        let salt = SaltString::encode_b64(b"corlinman_test_salt_bytes_16").unwrap();
        Argon2::default()
            .hash_password(password.as_bytes(), &salt)
            .unwrap()
            .to_string()
    }

    fn make_state(user: Option<&str>, password: Option<&str>) -> AdminState {
        let mut cfg = Config::default();
        cfg.admin.username = user.map(str::to_string);
        cfg.admin.password_hash = password.map(hash_password);
        AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        )
        .with_session_store(Arc::new(AdminSessionStore::new(StdDuration::from_secs(
            300,
        ))))
    }

    async fn body_json(res: axum::response::Response) -> serde_json::Value {
        let bytes = to_bytes(res.into_body(), usize::MAX).await.unwrap();
        serde_json::from_slice(&bytes).unwrap()
    }

    #[tokio::test]
    async fn login_success_sets_cookie_and_returns_token() {
        let state = make_state(Some("admin"), Some("secret"));
        let app = router(state.clone());
        let res = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/login")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"username":"admin","password":"secret"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let cookie = res
            .headers()
            .get(header::SET_COOKIE)
            .expect("Set-Cookie present")
            .to_str()
            .unwrap()
            .to_string();
        assert!(cookie.starts_with(&format!("{SESSION_COOKIE_NAME}=")));
        assert!(cookie.contains("HttpOnly"));
        assert!(cookie.contains("SameSite=Strict"));
        assert!(cookie.contains("Path=/"));
        assert!(cookie.contains("Max-Age=300"));

        let body = body_json(res).await;
        assert!(body.get("token").and_then(|v| v.as_str()).is_some());
        assert_eq!(body.get("expires_in").and_then(|v| v.as_i64()), Some(300));
        assert_eq!(state.session_store.as_ref().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn login_wrong_password_is_401() {
        let state = make_state(Some("admin"), Some("secret"));
        let app = router(state);
        let res = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/login")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"username":"admin","password":"WRONG"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
        assert!(res.headers().get(header::SET_COOKIE).is_none());
    }

    #[tokio::test]
    async fn login_without_admin_config_is_503() {
        let state = make_state(None, None);
        let app = router(state);
        let res = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/login")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"username":"admin","password":"secret"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn logout_clears_cookie_and_invalidates_session() {
        let state = make_state(Some("admin"), Some("secret"));
        let token = state.session_store.as_ref().unwrap().create("admin".into());
        let app = router(state.clone());
        let res = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/logout")
                    .header(header::COOKIE, format!("{SESSION_COOKIE_NAME}={token}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::NO_CONTENT);
        let cookie = res
            .headers()
            .get(header::SET_COOKIE)
            .unwrap()
            .to_str()
            .unwrap();
        assert!(cookie.contains("Max-Age=0"));
        assert_eq!(state.session_store.as_ref().unwrap().len(), 0);
    }

    #[tokio::test]
    async fn me_returns_session_info_for_valid_cookie() {
        let state = make_state(Some("admin"), Some("secret"));
        let token = state.session_store.as_ref().unwrap().create("admin".into());
        let app = router(state);
        let res = app
            .oneshot(
                Request::builder()
                    .uri("/admin/me")
                    .header(header::COOKIE, format!("{SESSION_COOKIE_NAME}={token}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let body = body_json(res).await;
        assert_eq!(body.get("user").and_then(|v| v.as_str()), Some("admin"));
        assert!(body.get("created_at").is_some());
        assert!(body.get("expires_at").is_some());
    }

    #[tokio::test]
    async fn me_without_cookie_is_401() {
        let state = make_state(Some("admin"), Some("secret"));
        let app = router(state);
        let res = app
            .oneshot(
                Request::builder()
                    .uri("/admin/me")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
    }

    // ---- Onboarding ----------------------------------------------------

    fn empty_admin_state(path: std::path::PathBuf) -> AdminState {
        let cfg = Config::default();
        AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        )
        .with_session_store(Arc::new(AdminSessionStore::new(StdDuration::from_secs(
            300,
        ))))
        .with_config_path(path)
    }

    #[tokio::test]
    async fn onboard_first_run_writes_admin_block_and_swaps_snapshot() {
        let tmp = tempfile::TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let state = empty_admin_state(path.clone());
        assert!(state.config.load().admin.username.is_none());

        let app = router(state.clone());
        let res = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/onboard")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        r#"{"username":"alice","password":"goodpassphrase"}"#,
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);

        // In-memory snapshot updated, hash is real argon2id.
        let cfg = state.config.load_full();
        assert_eq!(cfg.admin.username.as_deref(), Some("alice"));
        let hash = cfg.admin.password_hash.clone().expect("hash present");
        assert!(hash.starts_with("$argon2id$"));
        assert!(argon2_verify("goodpassphrase", &hash));

        // File on disk: round-trips, no sentinel.
        let on_disk = tokio::fs::read_to_string(&path).await.unwrap();
        assert!(on_disk.contains("username = \"alice\""));
        assert!(!on_disk.contains("***REDACTED***"));
    }

    #[tokio::test]
    async fn onboard_when_already_configured_returns_409() {
        let mut cfg = Config::default();
        cfg.admin.username = Some("admin".into());
        cfg.admin.password_hash = Some(hash_password("secret"));
        let tmp = tempfile::TempDir::new().unwrap();
        let state = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        )
        .with_session_store(Arc::new(AdminSessionStore::new(StdDuration::from_secs(
            300,
        ))))
        .with_config_path(tmp.path().join("config.toml"));

        let app = router(state);
        let res = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/onboard")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        r#"{"username":"hacker","password":"newpassphrase"}"#,
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::CONFLICT);
        let body = body_json(res).await;
        assert_eq!(body["error"], "already_onboarded");
    }

    #[tokio::test]
    async fn onboard_rejects_weak_password() {
        let tmp = tempfile::TempDir::new().unwrap();
        let state = empty_admin_state(tmp.path().join("config.toml"));
        let app = router(state);
        let res = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/onboard")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"username":"alice","password":"short"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::UNPROCESSABLE_ENTITY);
        let body = body_json(res).await;
        assert_eq!(body["error"], "weak_password");
    }
}
