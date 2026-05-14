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
/// `/admin/me`, `/admin/onboard`, and `/admin/password` all mount
/// *without* the `require_admin` middleware — see the module doc. The
/// onboarding route is session-less by necessity (no admin exists yet);
/// the change-password route does its own session-cookie + old-password
/// check, so we keep all credential-rotation endpoints in one file for
/// easier audit.
pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/admin/login", post(login))
        .route("/admin/logout", post(logout))
        .route("/admin/me", get(me))
        .route("/admin/onboard", post(onboard))
        .route("/admin/password", post(change_password))
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

#[derive(Debug, Deserialize)]
pub struct ChangePasswordRequest {
    pub old_password: String,
    pub new_password: String,
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
///
/// PR-#2 review issue #2: the `is_some()` precondition and the
/// `persist_admin_credentials` write are guarded by
/// `AdminState::admin_write_lock` so two concurrent onboard requests
/// can't both pass the check and clobber each other. The lock is
/// held for the entire critical section, including the atomic file
/// write — cheap because onboard is a once-per-deploy event.
async fn onboard(State(state): State<AdminState>, Json(body): Json<OnboardRequest>) -> Response {
    // Validate the payload *before* taking the serialising lock so
    // ill-formed requests fail fast without blocking a legitimate
    // sibling onboard attempt.
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

    // Hold the admin-write lock across the precondition check + the
    // persist so a concurrent caller sees `already_onboarded` (or
    // serialises behind us) instead of also winning the race.
    let _guard = state.admin_write_lock.lock().await;

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

    persist_admin_credentials(&state, body.username.trim().to_string(), &body.password).await
}

/// `POST /admin/password` — rotate the admin password for the logged-in
/// operator. Requires a valid session cookie *and* the correct
/// `old_password` (argon2 verify). The new password is hashed with a
/// fresh salt and written to `config.toml` via the same atomic path the
/// onboarding flow uses.
///
/// Status codes:
///   - 200 `{status: "ok"}` on success.
///   - 401 `invalid_old_password` when the old password doesn't verify.
///   - 401 `unauthenticated` when no valid session cookie is present.
///   - 422 `weak_password` if the new password is shorter than
///     [`MIN_PASSWORD_LEN`].
///   - 503 `admin_not_configured` when no admin is set up yet (use the
///     onboarding endpoint instead).
async fn change_password(
    State(state): State<AdminState>,
    headers: HeaderMap,
    Json(body): Json<ChangePasswordRequest>,
) -> Response {
    // Auth: session cookie required. We deliberately do NOT accept
    // Basic auth here — rotating the password mid-Basic-request would
    // race with the next call still carrying the old `Authorization`
    // header.
    let Some(store) = state.session_store.as_ref() else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"error": "session_store_missing"})),
        )
            .into_response();
    };
    let session_user = headers
        .get(header::COOKIE)
        .and_then(|v| v.to_str().ok())
        .and_then(|c| extract_cookie(c, SESSION_COOKIE_NAME))
        .and_then(|tok| store.validate(&tok))
        .map(|s| s.user);
    let Some(session_user) = session_user else {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "unauthenticated"})),
        )
            .into_response();
    };

    // PR-#2 review issue #2: serialise the verify+write critical
    // section against the shared admin-write lock so a concurrent
    // password rotation (or a racing /admin/onboard, which shares
    // the same lock) can't observe a stale snapshot. Held until the
    // response returns; the contention window is small (single
    // argon2 hash + atomic file rename).
    let _guard = state.admin_write_lock.lock().await;

    let cfg = state.config.load_full();
    let Some(username) = cfg.admin.username.clone() else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"error": "admin_not_configured"})),
        )
            .into_response();
    };
    let Some(expected_hash) = cfg.admin.password_hash.clone() else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"error": "admin_not_configured"})),
        )
            .into_response();
    };

    // The session token must match the configured admin user — keeps
    // a stale cookie from rotating credentials after a username change.
    if session_user != username {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "session_user_mismatch"})),
        )
            .into_response();
    }

    if !argon2_verify(&body.old_password, &expected_hash) {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "invalid_old_password"})),
        )
            .into_response();
    }
    if body.new_password.len() < MIN_PASSWORD_LEN {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({
                "error": "weak_password",
                "message": format!("password must be at least {MIN_PASSWORD_LEN} characters"),
            })),
        )
            .into_response();
    }

    persist_admin_credentials(&state, username, &body.new_password).await
}

/// Shared write path used by `onboard` and `change_password`. Hashes
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
    // Refresh the [meta] stamps before serialising so the audit trail
    // matches the on-disk write. `save_to_path` does this for the
    // boot-time loader, but the gateway's atomic-rename path bypasses
    // that helper — call the same factored-out method directly.
    new_cfg.stamp_meta();

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
        // PR-#2 review fix: persist_admin_credentials must stamp the
        // [meta] block before serialise so the audit trail matches.
        assert_eq!(
            cfg.meta.last_touched_version.as_deref(),
            Some(env!("CARGO_PKG_VERSION")),
        );
        let ts = cfg
            .meta
            .last_touched_at
            .as_deref()
            .expect("stamp_meta sets last_touched_at on onboard");
        assert_ne!(ts, "unknown");

        // File on disk: round-trips, no sentinel.
        let on_disk = tokio::fs::read_to_string(&path).await.unwrap();
        assert!(on_disk.contains("username = \"alice\""));
        assert!(!on_disk.contains("***REDACTED***"));
        // [meta] block landed on disk too, not just in memory.
        assert!(
            on_disk.contains("last_touched_version"),
            "[meta] stamps must persist; got:\n{on_disk}"
        );
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

    // ---- Change password ----------------------------------------------

    fn configured_state_with_path(
        user: &str,
        password: &str,
        path: std::path::PathBuf,
    ) -> AdminState {
        let mut cfg = Config::default();
        cfg.admin.username = Some(user.into());
        cfg.admin.password_hash = Some(hash_password(password));
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
    async fn change_password_with_valid_session_and_old_password_succeeds() {
        let tmp = tempfile::TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let state = configured_state_with_path("admin", "oldpassword", path.clone());
        let token = state.session_store.as_ref().unwrap().create("admin".into());

        let app = router(state.clone());
        let res = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/password")
                    .header("content-type", "application/json")
                    .header(header::COOKIE, format!("{SESSION_COOKIE_NAME}={token}"))
                    .body(Body::from(
                        r#"{"old_password":"oldpassword","new_password":"brand_new_passphrase"}"#,
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);

        let cfg = state.config.load_full();
        let new_hash = cfg.admin.password_hash.clone().expect("hash present");
        assert!(argon2_verify("brand_new_passphrase", &new_hash));
        assert!(!argon2_verify("oldpassword", &new_hash));

        let on_disk = tokio::fs::read_to_string(&path).await.unwrap();
        assert!(on_disk.contains("password_hash"));
        assert!(!on_disk.contains("***REDACTED***"));
    }

    #[tokio::test]
    async fn change_password_with_wrong_old_password_is_401() {
        let tmp = tempfile::TempDir::new().unwrap();
        let state =
            configured_state_with_path("admin", "real-pass", tmp.path().join("config.toml"));
        let token = state.session_store.as_ref().unwrap().create("admin".into());
        let app = router(state.clone());
        let res = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/password")
                    .header("content-type", "application/json")
                    .header(header::COOKIE, format!("{SESSION_COOKIE_NAME}={token}"))
                    .body(Body::from(
                        r#"{"old_password":"WRONG","new_password":"goodpassphrase"}"#,
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
        let body = body_json(res).await;
        assert_eq!(body["error"], "invalid_old_password");
        // Snapshot unchanged.
        assert!(argon2_verify(
            "real-pass",
            state.config.load().admin.password_hash.as_deref().unwrap(),
        ));
    }

    #[tokio::test]
    async fn change_password_without_session_is_401() {
        let tmp = tempfile::TempDir::new().unwrap();
        let state =
            configured_state_with_path("admin", "real-pass", tmp.path().join("config.toml"));
        let app = router(state);
        let res = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/password")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        r#"{"old_password":"real-pass","new_password":"newpassphrase"}"#,
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
        let body = body_json(res).await;
        assert_eq!(body["error"], "unauthenticated");
    }

    /// PR-#2 review issue #2: two concurrent `/admin/onboard` calls
    /// against an empty state used to both pass the `is_none()`
    /// precondition and race each other's persists. The shared
    /// `admin_write_lock` now serialises the verify+write critical
    /// section, so the second caller observes the first caller's
    /// credentials and gets `409 already_onboarded` instead of a
    /// silent overwrite.
    #[tokio::test]
    async fn onboard_concurrent_calls_serialise_via_admin_write_lock() {
        let tmp = tempfile::TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let state = empty_admin_state(path.clone());
        let app1 = router(state.clone());
        let app2 = router(state.clone());

        let req1 = Request::builder()
            .method("POST")
            .uri("/admin/onboard")
            .header("content-type", "application/json")
            .body(Body::from(
                r#"{"username":"alice","password":"goodpassphrase"}"#,
            ))
            .unwrap();
        let req2 = Request::builder()
            .method("POST")
            .uri("/admin/onboard")
            .header("content-type", "application/json")
            .body(Body::from(
                r#"{"username":"mallory","password":"otherpassphrase"}"#,
            ))
            .unwrap();

        // Fire both calls in parallel — the tokio mutex must serialise
        // their critical sections so exactly one wins.
        let (r1, r2) = tokio::join!(app1.oneshot(req1), app2.oneshot(req2));
        let r1 = r1.unwrap();
        let r2 = r2.unwrap();

        // Exactly one OK + one CONFLICT, in either order. Anything
        // else (two 200s, two 409s, or any other status) means the
        // lock isn't actually serialising the critical section.
        let codes = [r1.status(), r2.status()];
        let oks = codes.iter().filter(|s| **s == StatusCode::OK).count();
        let conflicts = codes.iter().filter(|s| **s == StatusCode::CONFLICT).count();
        assert_eq!(
            oks, 1,
            "expected exactly one OK across concurrent onboard calls; got {codes:?}",
        );
        assert_eq!(
            conflicts, 1,
            "expected exactly one CONFLICT across concurrent onboard calls; got {codes:?}",
        );

        // The winner's credentials landed; the loser's didn't clobber
        // them. We can't predict which is which, but the snapshot has
        // to match one of the two payloads exactly.
        let cfg = state.config.load_full();
        let user = cfg.admin.username.clone().expect("a winner persisted");
        assert!(
            user == "alice" || user == "mallory",
            "expected one of the two posted usernames, got {user:?}",
        );
        let hash = cfg.admin.password_hash.clone().expect("hash present");
        let winning_password = if user == "alice" {
            "goodpassphrase"
        } else {
            "otherpassphrase"
        };
        assert!(
            argon2_verify(winning_password, &hash),
            "stored hash should verify the winning password",
        );
    }

    #[tokio::test]
    async fn change_password_rejects_weak_new_password() {
        let tmp = tempfile::TempDir::new().unwrap();
        let state =
            configured_state_with_path("admin", "real-pass", tmp.path().join("config.toml"));
        let token = state.session_store.as_ref().unwrap().create("admin".into());
        let app = router(state);
        let res = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/password")
                    .header("content-type", "application/json")
                    .header(header::COOKIE, format!("{SESSION_COOKIE_NAME}={token}"))
                    .body(Body::from(
                        r#"{"old_password":"real-pass","new_password":"short"}"#,
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::UNPROCESSABLE_ENTITY);
        let body = body_json(res).await;
        assert_eq!(body["error"], "weak_password");
    }
}
