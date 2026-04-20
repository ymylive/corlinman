//! Auth guard for `/admin/*`.
//!
//! Two credentials get you past the guard, checked in order:
//!   1. `Cookie: corlinman_session=<token>` validated against
//!      [`super::admin_session::AdminSessionStore`] (S5 T1) — the normal
//!      UI path after `/admin/login`.
//!   2. `Authorization: Basic base64(user:pass)` verified against
//!      `config.admin.username` + `password_hash` (argon2id) — kept as a
//!      fallback for curl / CI / the initial login request itself.
//!
//! Both paths short-circuit the other: a cookie hit skips Basic entirely,
//! and missing/expired cookie falls through to Basic rather than 401. This
//! keeps the old M6 contract (HTTP Basic works) intact while letting the
//! UI authenticate once and rely on the cookie from then on.
//!
//! `AdminAuthState` clones cheaply (all fields are `Arc`), so rotating the
//! admin password or invalidating a session takes effect on the next
//! request without restart.

use std::sync::Arc;

use arc_swap::ArcSwap;
use argon2::{password_hash::PasswordHash, Argon2, PasswordVerifier};
use axum::{
    body::Body,
    extract::State,
    http::{header, Request, StatusCode},
    middleware::Next,
    response::{IntoResponse, Response},
    Json,
};
use base64::Engine;
use corlinman_core::config::Config;
use serde_json::json;

use super::admin_session::AdminSessionStore;

/// Cookie name carrying the opaque session token issued by `/admin/login`.
/// Exported so the login/logout handlers can write exactly the same name.
pub const SESSION_COOKIE_NAME: &str = "corlinman_session";

/// Cloneable bundle of state that the admin auth middleware + admin handlers
/// need. `config` is shared with the rest of the gateway; verifying per
/// request lets admins rotate credentials without a restart. `session_store`
/// is `None` in legacy / test setups that only want Basic auth — the
/// middleware then falls straight through to the Basic path.
#[derive(Clone)]
pub struct AdminAuthState {
    pub config: Arc<ArcSwap<Config>>,
    pub session_store: Option<Arc<AdminSessionStore>>,
}

impl AdminAuthState {
    pub fn new(config: Arc<ArcSwap<Config>>) -> Self {
        Self {
            config,
            session_store: None,
        }
    }

    /// Fluent: attach a session store so cookie auth is accepted.
    pub fn with_session_store(mut self, store: Arc<AdminSessionStore>) -> Self {
        self.session_store = Some(store);
        self
    }
}

/// Response payload for a 401. Matches the shape used by the rest of the
/// gateway so the UI's `CorlinmanApiError` parser can display a useful
/// message.
fn unauthorized(reason: &'static str) -> Response {
    (
        StatusCode::UNAUTHORIZED,
        [(header::WWW_AUTHENTICATE, r#"Basic realm="corlinman-admin""#)],
        Json(json!({
            "error": "unauthorized",
            "reason": reason,
        })),
    )
        .into_response()
}

/// Parse `Authorization: Basic <base64>` → `(user, pass)`. Returns `None`
/// for any malformed or non-Basic header.
fn parse_basic(header_value: &str) -> Option<(String, String)> {
    let rest = header_value.strip_prefix("Basic ")?.trim();
    let decoded = base64::engine::general_purpose::STANDARD
        .decode(rest)
        .ok()?;
    let s = String::from_utf8(decoded).ok()?;
    let (user, pass) = s.split_once(':')?;
    Some((user.to_string(), pass.to_string()))
}

/// Extract a named cookie value from a `Cookie:` header. Scan manually so we
/// don't pull a cookie parser crate for two fields. Returns `None` if the
/// header is absent or the cookie isn't present.
pub(crate) fn extract_cookie(header_value: &str, name: &str) -> Option<String> {
    for part in header_value.split(';') {
        let part = part.trim();
        if let Some((k, v)) = part.split_once('=') {
            if k.trim() == name {
                return Some(v.trim().to_string());
            }
        }
    }
    None
}

/// Verify `password` against an argon2id hash string. Any parse / verify
/// failure yields `false`; we never distinguish "wrong password" from
/// "malformed stored hash" in the response to avoid leaking hash shape.
pub(crate) fn argon2_verify(password: &str, stored_hash: &str) -> bool {
    let Ok(parsed) = PasswordHash::new(stored_hash) else {
        tracing::warn!("admin.password_hash is not a valid argon2 PHC string");
        return false;
    };
    Argon2::default()
        .verify_password(password.as_bytes(), &parsed)
        .is_ok()
}

/// Axum middleware function. Attach via
/// `Router::layer(from_fn_with_state(state, require_admin))`.
pub async fn require_admin(
    State(state): State<AdminAuthState>,
    req: Request<Body>,
    next: Next,
) -> Response {
    let cfg = state.config.load();
    let Some(expected_user) = cfg.admin.username.as_deref() else {
        // No admin credentials configured at all — fail closed.
        return unauthorized("admin_not_configured");
    };
    let Some(expected_hash) = cfg.admin.password_hash.as_deref() else {
        return unauthorized("admin_not_configured");
    };

    // 1) Cookie path — only if a session store is wired.
    if let Some(store) = state.session_store.as_ref() {
        if let Some(cookie_header) = req
            .headers()
            .get(header::COOKIE)
            .and_then(|v| v.to_str().ok())
        {
            if let Some(token) = extract_cookie(cookie_header, SESSION_COOKIE_NAME) {
                if store.validate(&token).is_some() {
                    return next.run(req).await;
                }
                // Cookie present but invalid/expired: fall through to
                // Basic auth rather than 401 so curl/CI still works.
            }
        }
    }

    // 2) Basic auth fallback.
    let Some(auth_header) = req
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
    else {
        return unauthorized("missing_authorization");
    };

    let Some((user, pass)) = parse_basic(auth_header) else {
        return unauthorized("malformed_authorization");
    };

    if user != expected_user {
        return unauthorized("invalid_credentials");
    }
    if !argon2_verify(&pass, expected_hash) {
        return unauthorized("invalid_credentials");
    }

    next.run(req).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use arc_swap::ArcSwap;
    use argon2::password_hash::{PasswordHasher, SaltString};
    use axum::body::Body;
    use axum::http::Request;
    use axum::routing::get;
    use axum::Router;
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

    fn state_with(user: Option<&str>, password: Option<&str>) -> AdminAuthState {
        let mut cfg = Config::default();
        cfg.admin.username = user.map(str::to_string);
        cfg.admin.password_hash = password.map(hash_password);
        AdminAuthState::new(Arc::new(ArcSwap::from_pointee(cfg)))
    }

    fn state_with_session(
        user: Option<&str>,
        password: Option<&str>,
        store: Arc<AdminSessionStore>,
    ) -> AdminAuthState {
        state_with(user, password).with_session_store(store)
    }

    fn app(state: AdminAuthState) -> Router {
        Router::new()
            .route("/ping", get(|| async { "pong" }))
            .layer(axum::middleware::from_fn_with_state(state, require_admin))
    }

    fn basic_header(user: &str, pass: &str) -> String {
        let raw = format!("{user}:{pass}");
        format!(
            "Basic {}",
            base64::engine::general_purpose::STANDARD.encode(raw)
        )
    }

    #[tokio::test]
    async fn missing_header_is_401() {
        let app = app(state_with(Some("admin"), Some("secret")));
        let res = app
            .oneshot(Request::builder().uri("/ping").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
        assert!(res.headers().contains_key(header::WWW_AUTHENTICATE));
    }

    #[tokio::test]
    async fn wrong_password_is_401() {
        let app = app(state_with(Some("admin"), Some("secret")));
        let res = app
            .oneshot(
                Request::builder()
                    .uri("/ping")
                    .header(header::AUTHORIZATION, basic_header("admin", "WRONG"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn wrong_username_is_401() {
        let app = app(state_with(Some("admin"), Some("secret")));
        let res = app
            .oneshot(
                Request::builder()
                    .uri("/ping")
                    .header(header::AUTHORIZATION, basic_header("root", "secret"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn correct_credentials_pass_through() {
        let app = app(state_with(Some("admin"), Some("secret")));
        let res = app
            .oneshot(
                Request::builder()
                    .uri("/ping")
                    .header(header::AUTHORIZATION, basic_header("admin", "secret"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn no_admin_configured_is_401() {
        let app = app(state_with(None, None));
        let res = app
            .oneshot(
                Request::builder()
                    .uri("/ping")
                    .header(header::AUTHORIZATION, basic_header("admin", "secret"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
    }

    #[test]
    fn parse_basic_accepts_well_formed() {
        let h = basic_header("alice", "hunter2");
        let (u, p) = parse_basic(&h).unwrap();
        assert_eq!(u, "alice");
        assert_eq!(p, "hunter2");
    }

    #[test]
    fn parse_basic_rejects_non_basic() {
        assert!(parse_basic("Bearer xyz").is_none());
        assert!(parse_basic("Basic @@@not-base64@@@").is_none());
    }

    // --- cookie / session_store branch -----------------------------------

    #[test]
    fn extract_cookie_finds_named_value() {
        assert_eq!(
            extract_cookie("foo=bar; corlinman_session=abc123", SESSION_COOKIE_NAME),
            Some("abc123".into())
        );
        assert_eq!(
            extract_cookie("corlinman_session=xyz", SESSION_COOKIE_NAME),
            Some("xyz".into())
        );
        assert!(extract_cookie("foo=bar", SESSION_COOKIE_NAME).is_none());
    }

    #[tokio::test]
    async fn valid_cookie_lets_request_through() {
        let store = Arc::new(AdminSessionStore::new(StdDuration::from_secs(60)));
        let token = store.create("admin".into());
        let state = state_with_session(Some("admin"), Some("secret"), store);
        let res = app(state)
            .oneshot(
                Request::builder()
                    .uri("/ping")
                    .header(header::COOKIE, format!("{SESSION_COOKIE_NAME}={token}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn expired_cookie_falls_through_to_basic_auth() {
        // 0s TTL ⇒ token is already expired.
        let store = Arc::new(AdminSessionStore::new(StdDuration::from_secs(0)));
        let token = store.create("admin".into());
        tokio::time::sleep(StdDuration::from_millis(1100)).await;
        let state = state_with_session(Some("admin"), Some("secret"), store);

        // Expired cookie + valid Basic auth should succeed.
        let res = app(state.clone())
            .oneshot(
                Request::builder()
                    .uri("/ping")
                    .header(header::COOKIE, format!("{SESSION_COOKIE_NAME}={token}"))
                    .header(header::AUTHORIZATION, basic_header("admin", "secret"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);

        // Expired cookie + no Basic auth → 401.
        let res = app(state)
            .oneshot(
                Request::builder()
                    .uri("/ping")
                    .header(header::COOKIE, format!("{SESSION_COOKIE_NAME}={token}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn cookie_takes_priority_over_basic_when_both_present() {
        let store = Arc::new(AdminSessionStore::new(StdDuration::from_secs(60)));
        let token = store.create("admin".into());
        let state = state_with_session(Some("admin"), Some("secret"), store);

        // Bogus Basic creds — the cookie should still let us through.
        let res = app(state)
            .oneshot(
                Request::builder()
                    .uri("/ping")
                    .header(header::COOKIE, format!("{SESSION_COOKIE_NAME}={token}"))
                    .header(header::AUTHORIZATION, basic_header("admin", "WRONG"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn basic_auth_still_works_when_no_session_store() {
        // Mirrors the pre-S5 contract: even without a session store
        // wired, a valid Basic-auth request must pass.
        let state = state_with(Some("admin"), Some("secret"));
        let res = app(state)
            .oneshot(
                Request::builder()
                    .uri("/ping")
                    .header(header::AUTHORIZATION, basic_header("admin", "secret"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
    }
}
