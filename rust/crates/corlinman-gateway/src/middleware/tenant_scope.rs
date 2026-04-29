//! Tenant-scoping middleware for `/admin/*`.
//!
//! Phase 4 W1 4-1A Item 3. Resolves an inbound admin request to a single
//! [`TenantId`] and stashes it in the request extensions so handlers can
//! pull it back via the [`Tenant`] extractor. Two policy modes, decided
//! once at boot from the `[tenants].enabled` config switch:
//!
//! - **Disabled (legacy single-tenant)**: every request gets
//!   [`TenantId::legacy_default`] without consulting the request — the
//!   middleware is effectively transparent. This is the byte-for-byte
//!   pre-Phase-4 behaviour.
//! - **Enabled (multi-tenant)**: the middleware extracts a candidate
//!   slug from `?tenant=<slug>` (Phase 4 W1 only — session-cookie tenant
//!   claims land in a Phase 4 W1 follow-up alongside the
//!   `AdminSessionStore` schema bump). The slug is parsed through
//!   [`TenantId::new`] and validated against the operator-allowed set
//!   stored on `TenantScopeState`. Empty / missing query falls back to
//!   the configured default tenant.
//!
//! Two short-circuit error paths:
//!
//! - HTTP **400 `invalid_tenant_slug`** when the query carries a slug
//!   that fails the `^[a-z][a-z0-9-]{0,62}$` shape — surfaced clearly so
//!   the UI can distinguish a typo from an authorisation failure.
//! - HTTP **403 `tenant_not_allowed`** when the slug parses but is not
//!   in `state.allowed`. This is the security boundary that satisfies
//!   the Wave 1 acceptance: an operator scoped to tenant A who tries
//!   `?tenant=B` sees a 403, not a silently empty result.
//!
//! Mount order: this layer sits *inside* `require_admin` (so anonymous
//! callers never see `tenant_not_allowed`; they get 401 first) and
//! *outside* the per-route handlers (so handlers always observe a
//! resolved `TenantId` in extensions).

use std::collections::BTreeSet;
use std::sync::Arc;

use axum::{
    body::Body,
    extract::{FromRequestParts, State},
    http::{request::Parts, Request, StatusCode},
    middleware::Next,
    response::{IntoResponse, Response},
    Json,
};
use corlinman_tenant::TenantId;
use serde_json::json;

/// Boot-time state for [`tenant_scope`]. `enabled` mirrors
/// `Config::tenants::enabled` at gateway start; `allowed` is the union
/// of `[tenants].allowed` slugs (validated at boot) plus
/// [`TenantId::legacy_default`]; `fallback` is the tenant returned
/// when the request omits `?tenant=` (matches `[tenants].default`).
///
/// Cloneable: every field is small or `Arc`-wrapped so the layer
/// can be stamped onto the admin router without per-clone allocation
/// pressure. Held by value on the layer; not stored in `AdminState`.
#[derive(Clone)]
pub struct TenantScopeState {
    pub enabled: bool,
    pub allowed: Arc<BTreeSet<TenantId>>,
    pub fallback: TenantId,
}

impl TenantScopeState {
    /// Convenience: build a disabled state where every request resolves
    /// to [`TenantId::legacy_default`]. Used by tests that want to
    /// assert handler behaviour without exercising tenant scoping.
    pub fn disabled() -> Self {
        Self {
            enabled: false,
            allowed: Arc::new(BTreeSet::new()),
            fallback: TenantId::legacy_default(),
        }
    }
}

/// Axum extractor: pulls the tenant id resolved by [`tenant_scope`]
/// out of request extensions. Returns 500 if missing — that's a wiring
/// bug (handler used before the middleware mounted), surfaced loudly
/// so it doesn't degrade silently to "default".
#[derive(Clone, Debug)]
pub struct Tenant(pub TenantId);

#[axum::async_trait]
impl<S> FromRequestParts<S> for Tenant
where
    S: Send + Sync,
{
    type Rejection = (StatusCode, Json<serde_json::Value>);

    async fn from_request_parts(parts: &mut Parts, _state: &S) -> Result<Self, Self::Rejection> {
        match parts.extensions.get::<TenantId>().cloned() {
            Some(t) => Ok(Tenant(t)),
            None => Err((
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({
                    "error": "tenant_extension_missing",
                    "hint": "tenant_scope middleware was not mounted before this handler",
                })),
            )),
        }
    }
}

/// Resolve the tenant for this request and stash it in extensions.
///
/// See module docs for the policy. On success the inner handler
/// observes `req.extensions().get::<TenantId>()` populated; on policy
/// failure the middleware short-circuits with 400 / 403 and the
/// handler never runs.
pub async fn tenant_scope(
    State(state): State<TenantScopeState>,
    mut req: Request<Body>,
    next: Next,
) -> Response {
    let tenant = if !state.enabled {
        state.fallback.clone()
    } else {
        match extract_tenant_query(req.uri().query().unwrap_or("")) {
            None => state.fallback.clone(),
            Some(raw) => match TenantId::new(&raw) {
                Err(err) => {
                    return (
                        StatusCode::BAD_REQUEST,
                        Json(json!({
                            "error": "invalid_tenant_slug",
                            "slug": raw,
                            "reason": err.to_string(),
                        })),
                    )
                        .into_response();
                }
                Ok(t) => {
                    if !state.allowed.contains(&t) {
                        return (
                            StatusCode::FORBIDDEN,
                            Json(json!({
                                "error": "tenant_not_allowed",
                                "slug": t.as_str(),
                            })),
                        )
                            .into_response();
                    }
                    t
                }
            },
        }
    };

    req.extensions_mut().insert(tenant);
    next.run(req).await
}

/// Extract the first `tenant=` value from a percent-encoded query
/// string. Returns the raw value (URL-decoded) without further
/// validation — the caller is responsible for parsing through
/// [`TenantId::new`].
///
/// Inlined rather than pulled from a query-parser crate to keep this
/// middleware free of new dependencies. The tenant query is single-
/// valued, the slug regex bans `=` and `&`, and we only need the
/// first match — so a hand-rolled scan over `&`-split pairs is enough.
fn extract_tenant_query(q: &str) -> Option<String> {
    for pair in q.split('&') {
        let mut it = pair.splitn(2, '=');
        let key = it.next()?;
        if key != "tenant" {
            continue;
        }
        let raw = it.next().unwrap_or("");
        // Minimal percent-decode for the characters slugs can contain.
        // Slugs are `[a-z0-9-]`, none of which need percent-encoding,
        // so a naive URL-decode is overkill; we just unescape `%2D`
        // (`-`) defensively in case a client over-encodes it. Anything
        // exotic falls through and `TenantId::new` will reject it.
        let unescaped = raw.replace("%2D", "-").replace("%2d", "-");
        return Some(unescaped);
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::to_bytes;
    use axum::routing::get;
    use axum::{middleware, Router};
    use tower::ServiceExt;

    /// Handler that pulls the tenant out via the extractor and echoes
    /// it back as JSON. Lets the tests assert on the resolved id end-
    /// to-end instead of poking extensions directly.
    async fn echo(Tenant(t): Tenant) -> Json<serde_json::Value> {
        Json(json!({"tenant": t.as_str()}))
    }

    fn app(state: TenantScopeState) -> Router {
        Router::new()
            .route("/probe", get(echo))
            .layer(middleware::from_fn_with_state(state, tenant_scope))
    }

    fn allowed(slugs: &[&str]) -> Arc<BTreeSet<TenantId>> {
        let mut s = BTreeSet::new();
        s.insert(TenantId::legacy_default());
        for slug in slugs {
            s.insert(TenantId::new(*slug).expect("test slug must be valid"));
        }
        Arc::new(s)
    }

    #[tokio::test]
    async fn disabled_resolves_every_request_to_default() {
        let app = app(TenantScopeState::disabled());
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/probe?tenant=acme")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = to_bytes(resp.into_body(), 1024).await.unwrap();
        let body: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(body["tenant"], "default");
    }

    #[tokio::test]
    async fn enabled_falls_back_to_configured_default_when_query_absent() {
        let state = TenantScopeState {
            enabled: true,
            allowed: allowed(&["acme"]),
            fallback: TenantId::legacy_default(),
        };
        let app = app(state);
        let resp = app
            .oneshot(Request::builder().uri("/probe").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body: serde_json::Value =
            serde_json::from_slice(&to_bytes(resp.into_body(), 1024).await.unwrap()).unwrap();
        assert_eq!(body["tenant"], "default");
    }

    #[tokio::test]
    async fn enabled_resolves_allowed_tenant_query() {
        let state = TenantScopeState {
            enabled: true,
            allowed: allowed(&["acme", "bravo"]),
            fallback: TenantId::legacy_default(),
        };
        let app = app(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/probe?tenant=bravo")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body: serde_json::Value =
            serde_json::from_slice(&to_bytes(resp.into_body(), 1024).await.unwrap()).unwrap();
        assert_eq!(body["tenant"], "bravo");
    }

    #[tokio::test]
    async fn enabled_rejects_invalid_slug_with_400() {
        let state = TenantScopeState {
            enabled: true,
            allowed: allowed(&["acme"]),
            fallback: TenantId::legacy_default(),
        };
        let app = app(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/probe?tenant=BAD!!")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let body: serde_json::Value =
            serde_json::from_slice(&to_bytes(resp.into_body(), 1024).await.unwrap()).unwrap();
        assert_eq!(body["error"], "invalid_tenant_slug");
    }

    /// The Wave 1 acceptance line: an operator targeting a tenant they
    /// have not been granted access to gets a 403, not a silently
    /// empty list.
    #[tokio::test]
    async fn enabled_rejects_disallowed_tenant_with_403() {
        let state = TenantScopeState {
            enabled: true,
            allowed: allowed(&["acme"]),
            fallback: TenantId::legacy_default(),
        };
        let app = app(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/probe?tenant=bravo")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::FORBIDDEN);
        let body: serde_json::Value =
            serde_json::from_slice(&to_bytes(resp.into_body(), 1024).await.unwrap()).unwrap();
        assert_eq!(body["error"], "tenant_not_allowed");
        assert_eq!(body["slug"], "bravo");
    }

    #[tokio::test]
    async fn missing_extension_returns_500_explicit_wiring_bug() {
        // Direct extractor call without the middleware — proves the
        // 500 path fires when the layer is not mounted, so a future
        // refactor that drops the layer fails loudly.
        async fn handler(t: Result<Tenant, (StatusCode, Json<serde_json::Value>)>) -> StatusCode {
            match t {
                Ok(_) => StatusCode::OK,
                Err((code, _)) => code,
            }
        }
        let app = Router::new().route("/probe", get(handler));
        let resp = app
            .oneshot(Request::builder().uri("/probe").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::INTERNAL_SERVER_ERROR);
    }

    #[test]
    fn extract_tenant_query_finds_first_match() {
        assert_eq!(extract_tenant_query("tenant=acme"), Some("acme".into()));
        assert_eq!(
            extract_tenant_query("foo=1&tenant=bravo&bar=2"),
            Some("bravo".into())
        );
        assert_eq!(extract_tenant_query("foo=1&bar=2"), None);
        assert_eq!(extract_tenant_query(""), None);
        // Tolerates over-encoded `-` even though slugs don't strictly
        // need it.
        assert_eq!(
            extract_tenant_query("tenant=ac%2Dme"),
            Some("ac-me".into())
        );
    }
}
