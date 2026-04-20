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

/// Router for the three session routes. These mount *without* the
/// `require_admin` middleware — see the module doc.
pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/admin/login", post(login))
        .route("/admin/logout", post(logout))
        .route("/admin/me", get(me))
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
}
