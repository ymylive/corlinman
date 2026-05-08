//! `/admin/identity*` — operator-facing identity surface
//! (Phase 4 W2 B2 iter 6).
//!
//! Four routes, all behind `require_admin` and `tenant_scope`:
//!
//! - `GET  /admin/identity?limit=&offset=` — paginated list of users
//!   in this tenant's identity graph. Mirrors the
//!   [`UserSummary`](corlinman_identity::UserSummary) wire shape.
//! - `GET  /admin/identity/:user_id` — detail view for one user;
//!   returns every alias bound to that `user_id`.
//! - `POST /admin/identity/:user_id/issue-phrase` — issue a fresh
//!   verification phrase for a `(channel, channel_user_id)` pair the
//!   operator has confirmed maps to `user_id`. The 201 body echoes
//!   the phrase + `expires_at` so the admin UI can show it.
//! - `POST /admin/identity/merge` — operator-driven manual merge.
//!   Reattributes every alias on `from_user_id` to `into_user_id`
//!   (`binding_kind = 'operator'`) and deletes the orphaned source
//!   row. Audit breadcrumb via `decided_by` (logged for now; the
//!   audit-log surface lands in a follow-up).
//!
//! ### Disabled / not-found paths
//!
//! - **503 `identity_disabled`** when `AdminState::identity_store`
//!   is `None`. The UI keys off the 503 status to render the
//!   "identity store is off" banner. All four routes share the gate.
//! - **404 `not_found`** with `user_id` echoed back when the detail
//!   route can't find a matching row.
//! - **400 `invalid_input`** for empty/malformed bodies on the two
//!   POST routes.
//!
//! ### Tenant scoping
//!
//! `IdentityStore` is store-per-file scoped (per-tenant SQLite at
//! `<data_dir>/tenants/<slug>/user_identity.sqlite`), so handlers
//! don't take a per-call tenant arg the way `sessions.rs` does. The
//! `AdminState::identity_store` handle is opened against one tenant
//! at boot. Multi-tenant chat-side scoping pairs with the sessions
//! convention and lands when the boot path opens a per-tenant store
//! (out of scope for iter 6 — boot wiring is iter 8 in the design).

use std::sync::Arc;

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use corlinman_identity::{ChannelAlias, IdentityError, IdentityStore, UserId, UserSummary};
use serde::{Deserialize, Serialize};
use serde_json::json;
use time::format_description::well_known::Rfc3339;
use tracing::warn;

use super::AdminState;

/* ------------------------------------------------------------------ */
/*                        Wire shapes                                  */
/* ------------------------------------------------------------------ */

/// Wire shape for `GET /admin/identity`. Mirrors the UI's
/// `IdentityListResponse` (lands in iter 7 under
/// `ui/lib/api/identity.ts`).
#[derive(Debug, Serialize, Deserialize)]
pub struct IdentityListOut {
    pub users: Vec<UserSummary>,
}

/// Pagination query for `GET /admin/identity`. Defaults match the
/// design doc: 50 rows per page, offset 0.
#[derive(Debug, Deserialize)]
pub struct ListQuery {
    #[serde(default)]
    pub limit: Option<u32>,
    #[serde(default)]
    pub offset: Option<u32>,
}

/// One alias as it appears on the wire. Distinct from
/// [`ChannelAlias`] only because we serialise `binding_kind` and
/// `created_at` as their stable string forms — the typed enum derives
/// `serde` already, so this is a thin wire envelope rather than a
/// remap.
#[derive(Debug, Serialize, Deserialize)]
pub struct AliasOut {
    pub channel: String,
    pub channel_user_id: String,
    pub user_id: String,
    pub binding_kind: String,
    /// RFC-3339 string. The UI parses with `new Date(ts)`.
    pub created_at: String,
}

impl AliasOut {
    fn from_alias(a: ChannelAlias) -> Self {
        Self {
            channel: a.channel,
            channel_user_id: a.channel_user_id,
            user_id: a.user_id.as_str().to_string(),
            binding_kind: a.binding_kind.as_str().to_string(),
            created_at: a
                .created_at
                .format(&Rfc3339)
                .unwrap_or_else(|_| "1970-01-01T00:00:00Z".to_string()),
        }
    }
}

/// Wire shape for `GET /admin/identity/:user_id`. Mirrors the UI's
/// `IdentityDetailResponse`.
#[derive(Debug, Serialize, Deserialize)]
pub struct IdentityDetailOut {
    pub user_id: String,
    pub aliases: Vec<AliasOut>,
}

/// Body for `POST /admin/identity/:user_id/issue-phrase`. Mirrors
/// the UI's `IssuePhraseBody`.
#[derive(Debug, Deserialize)]
pub struct IssuePhraseBody {
    pub channel: String,
    pub channel_user_id: String,
}

/// Wire shape for the 201 response of `POST
/// /admin/identity/:user_id/issue-phrase`. The phrase is echoed back
/// so the admin UI can present it to the operator alongside a "send
/// this on the other channel" hint.
#[derive(Debug, Serialize, Deserialize)]
pub struct IssuePhraseOut {
    pub phrase: String,
    pub user_id: String,
    /// RFC-3339 string. UI shows the time-to-expiry countdown.
    pub expires_at: String,
}

/// Body for `POST /admin/identity/merge`. Operator-driven manual
/// merge — `decided_by` is the operator's username, retained for
/// audit even though the audit-log surface itself ships later.
#[derive(Debug, Deserialize)]
pub struct MergeBody {
    pub into_user_id: String,
    pub from_user_id: String,
    pub decided_by: String,
}

/// Wire shape for the 200 response of `POST /admin/identity/merge`.
#[derive(Debug, Serialize, Deserialize)]
pub struct MergeOut {
    pub surviving_user_id: String,
}

/* ------------------------------------------------------------------ */
/*                       Router + helpers                              */
/* ------------------------------------------------------------------ */

/// Sub-router for `/admin/identity*`. Mounted by
/// [`super::router_with_state`] inside `require_admin` + `tenant_scope`.
pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/admin/identity", get(list_handler))
        .route("/admin/identity/:user_id", get(detail_handler))
        .route(
            "/admin/identity/:user_id/issue-phrase",
            post(issue_phrase_handler),
        )
        .route("/admin/identity/merge", post(merge_handler))
        .with_state(state)
}

fn identity_disabled_503() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({
            "error": "identity_disabled",
        })),
    )
        .into_response()
}

fn user_not_found_404(user_id: &str) -> Response {
    (
        StatusCode::NOT_FOUND,
        Json(json!({
            "error": "not_found",
            "user_id": user_id,
        })),
    )
        .into_response()
}

fn invalid_input_400(message: &str) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({
            "error": "invalid_input",
            "message": message,
        })),
    )
        .into_response()
}

fn storage_error(err: impl std::fmt::Display, ctx: &'static str) -> Response {
    warn!(error = %err, "admin/identity {ctx} failed");
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        Json(json!({
            "error": "storage_error",
            "message": err.to_string(),
        })),
    )
        .into_response()
}

/// Borrow the identity store off `AdminState`.
/// All four handlers funnel through this so the disabled gate is
/// enforced exactly once per route.
fn require_store(state: &AdminState) -> Option<Arc<dyn IdentityStore>> {
    state.identity_store.clone()
}

/* ------------------------------------------------------------------ */
/*                            Handlers                                 */
/* ------------------------------------------------------------------ */

async fn list_handler(State(state): State<AdminState>, Query(q): Query<ListQuery>) -> Response {
    let Some(store) = require_store(&state) else {
        return identity_disabled_503();
    };
    // Defaults: 50 rows per page, offset 0. The store clamps `limit`
    // to `[1, 200]` itself, so we don't pre-clamp here — the route
    // layer just supplies sensible defaults for missing params.
    let limit = q.limit.unwrap_or(50);
    let offset = q.offset.unwrap_or(0);
    match store.list_users(limit, offset).await {
        Ok(users) => (StatusCode::OK, Json(IdentityListOut { users })).into_response(),
        Err(other) => storage_error(other, "list"),
    }
}

async fn detail_handler(State(state): State<AdminState>, Path(user_id): Path<String>) -> Response {
    let Some(store) = require_store(&state) else {
        return identity_disabled_503();
    };
    let uid = UserId::from(user_id.clone());
    let aliases = match store.aliases_for(&uid).await {
        Ok(a) => a,
        Err(err) => return storage_error(err, "detail_aliases"),
    };
    if aliases.is_empty() {
        // 404 when there's no row at all — the detail page must
        // distinguish "user has zero aliases" (rare but possible
        // post-merge edge case) from "user_id doesn't exist". Today
        // an `Auto`-bound user always has ≥ 1 alias, so an empty
        // result is the safe 404 trigger; if the store ever ships
        // alias-less users the contract widens and this turns into
        // a tri-state.
        return user_not_found_404(&user_id);
    }
    let out = IdentityDetailOut {
        user_id,
        aliases: aliases.into_iter().map(AliasOut::from_alias).collect(),
    };
    (StatusCode::OK, Json(out)).into_response()
}

async fn issue_phrase_handler(
    State(state): State<AdminState>,
    Path(user_id): Path<String>,
    body: Option<Json<IssuePhraseBody>>,
) -> Response {
    let Some(store) = require_store(&state) else {
        return identity_disabled_503();
    };
    // Body is required; an empty body is a programming error in the
    // admin UI, not a degraded path — surface it as 400 explicitly.
    let Some(Json(body)) = body else {
        return invalid_input_400("body must include channel and channel_user_id");
    };
    if body.channel.trim().is_empty() {
        return invalid_input_400("channel must be non-empty");
    }
    if body.channel_user_id.trim().is_empty() {
        return invalid_input_400("channel_user_id must be non-empty");
    }
    if user_id.trim().is_empty() {
        return invalid_input_400("user_id path segment must be non-empty");
    }

    let uid = UserId::from(user_id.clone());
    match store
        .issue_phrase(&uid, &body.channel, &body.channel_user_id)
        .await
    {
        Ok(phrase) => {
            let expires_at = phrase
                .expires_at
                .format(&Rfc3339)
                .unwrap_or_else(|_| "1970-01-01T00:00:00Z".to_string());
            (
                StatusCode::CREATED,
                Json(IssuePhraseOut {
                    phrase: phrase.phrase,
                    user_id,
                    expires_at,
                }),
            )
                .into_response()
        }
        Err(IdentityError::InvalidInput(msg)) => invalid_input_400(msg),
        Err(other) => storage_error(other, "issue_phrase"),
    }
}

async fn merge_handler(State(state): State<AdminState>, body: Option<Json<MergeBody>>) -> Response {
    let Some(store) = require_store(&state) else {
        return identity_disabled_503();
    };
    let Some(Json(body)) = body else {
        return invalid_input_400("body must include into_user_id, from_user_id, decided_by");
    };
    if body.into_user_id.trim().is_empty() {
        return invalid_input_400("into_user_id must be non-empty");
    }
    if body.from_user_id.trim().is_empty() {
        return invalid_input_400("from_user_id must be non-empty");
    }
    if body.decided_by.trim().is_empty() {
        return invalid_input_400("decided_by must be non-empty");
    }
    let into = UserId::from(body.into_user_id);
    let from = UserId::from(body.from_user_id);

    match store.merge_users(&into, &from, &body.decided_by).await {
        Ok(surviving) => (
            StatusCode::OK,
            Json(MergeOut {
                surviving_user_id: surviving.as_str().to_string(),
            }),
        )
            .into_response(),
        Err(IdentityError::InvalidInput(msg)) => invalid_input_400(msg),
        Err(IdentityError::UserNotFound(uid)) => user_not_found_404(&uid),
        Err(other) => storage_error(other, "merge"),
    }
}

/* ------------------------------------------------------------------ */
/*                              Tests                                  */
/* ------------------------------------------------------------------ */

#[cfg(test)]
mod tests {
    use super::*;
    use arc_swap::ArcSwap;
    use axum::body::{to_bytes, Body};
    use axum::http::Request;
    use corlinman_core::config::Config;
    use corlinman_identity::{identity_db_path, SqliteIdentityStore};
    use corlinman_plugins::registry::PluginRegistry;
    use corlinman_tenant::TenantId;
    use std::sync::Arc;
    use tempfile::TempDir;
    use time::OffsetDateTime;
    use tower::ServiceExt;

    /// Build a fresh per-tempdir store and wrap it in a minimal
    /// `AdminState`. Pool size 1 mirrors the identity-crate convention
    /// for dodging the WAL cross-conn visibility race.
    async fn fresh(tmp: &TempDir) -> (AdminState, Arc<SqliteIdentityStore>) {
        let tenant = TenantId::legacy_default();
        let path = identity_db_path(tmp.path(), &tenant);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let store = Arc::new(
            SqliteIdentityStore::open_with_pool_size(&path, 1)
                .await
                .unwrap(),
        );
        let state = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(Config::default())),
        )
        .with_identity_store(store.clone() as Arc<dyn IdentityStore>);
        (state, store)
    }

    /// Build a state with no identity store — the disabled-gate path.
    fn disabled_state() -> AdminState {
        AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(Config::default())),
        )
    }

    /* ------------------------------- list ------------------------------- */

    #[tokio::test]
    async fn list_returns_users_with_alias_counts() {
        let tmp = TempDir::new().unwrap();
        let (state, store) = fresh(&tmp).await;
        let _ = store.resolve_or_create("qq", "1", None).await.unwrap();
        let _ = store
            .resolve_or_create("qq", "2", Some("Bob"))
            .await
            .unwrap();

        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/identity")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: IdentityListOut = serde_json::from_slice(&body).unwrap();
        assert_eq!(v.users.len(), 2);
        // Most-recently-minted user lands first.
        assert_eq!(v.users[0].display_name.as_deref(), Some("Bob"));
        assert_eq!(v.users[0].alias_count, 1);
    }

    #[tokio::test]
    async fn list_paginates_via_query_string() {
        let tmp = TempDir::new().unwrap();
        let (state, store) = fresh(&tmp).await;
        for i in 0..5 {
            store
                .resolve_or_create("qq", &i.to_string(), None)
                .await
                .unwrap();
        }
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/identity?limit=2&offset=2")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: IdentityListOut = serde_json::from_slice(&body).unwrap();
        assert_eq!(v.users.len(), 2);
    }

    #[tokio::test]
    async fn list_returns_503_when_disabled() {
        let app = router(disabled_state());
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/identity")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["error"], "identity_disabled");
    }

    /* ------------------------------ detail ------------------------------ */

    #[tokio::test]
    async fn detail_returns_aliases_for_known_user() {
        let tmp = TempDir::new().unwrap();
        let (state, store) = fresh(&tmp).await;
        let uid = store.resolve_or_create("qq", "1234", None).await.unwrap();

        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri(format!("/admin/identity/{}", uid.as_str()))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: IdentityDetailOut = serde_json::from_slice(&body).unwrap();
        assert_eq!(v.user_id, uid.as_str());
        assert_eq!(v.aliases.len(), 1);
        assert_eq!(v.aliases[0].channel, "qq");
        assert_eq!(v.aliases[0].channel_user_id, "1234");
        assert_eq!(v.aliases[0].binding_kind, "auto");
    }

    #[tokio::test]
    async fn detail_returns_404_for_unknown_user() {
        let tmp = TempDir::new().unwrap();
        let (state, _store) = fresh(&tmp).await;
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/identity/01HV3K9PQRSTUVWXYZABCDEFGH")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["error"], "not_found");
        assert_eq!(v["user_id"], "01HV3K9PQRSTUVWXYZABCDEFGH");
    }

    #[tokio::test]
    async fn detail_returns_503_when_disabled() {
        let app = router(disabled_state());
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/identity/whatever")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    /* --------------------------- issue-phrase --------------------------- */

    #[tokio::test]
    async fn issue_phrase_returns_201_with_phrase_and_expiry() {
        let tmp = TempDir::new().unwrap();
        let (state, store) = fresh(&tmp).await;
        let uid = store.resolve_or_create("qq", "1234", None).await.unwrap();

        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri(format!("/admin/identity/{}/issue-phrase", uid.as_str()))
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"channel":"qq","channel_user_id":"1234"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::CREATED);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: IssuePhraseOut = serde_json::from_slice(&body).unwrap();
        assert_eq!(v.user_id, uid.as_str());
        assert_eq!(v.phrase.len(), 11, "9 chars + 2 hyphens");
        // The expires_at parses as an RFC-3339 timestamp in the future.
        let expires = OffsetDateTime::parse(&v.expires_at, &Rfc3339).expect("rfc3339 timestamp");
        assert!(expires > OffsetDateTime::now_utc());
    }

    #[tokio::test]
    async fn issue_phrase_returns_400_for_empty_channel() {
        let tmp = TempDir::new().unwrap();
        let (state, store) = fresh(&tmp).await;
        let uid = store.resolve_or_create("qq", "1234", None).await.unwrap();
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri(format!("/admin/identity/{}/issue-phrase", uid.as_str()))
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"channel":"","channel_user_id":"1234"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["error"], "invalid_input");
    }

    #[tokio::test]
    async fn issue_phrase_returns_503_when_disabled() {
        let app = router(disabled_state());
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/identity/whatever/issue-phrase")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"channel":"qq","channel_user_id":"1234"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    /* ------------------------------- merge ------------------------------ */

    #[tokio::test]
    async fn merge_unifies_two_users_and_returns_surviving_id() {
        let tmp = TempDir::new().unwrap();
        let (state, store) = fresh(&tmp).await;
        let into = store.resolve_or_create("qq", "1234", None).await.unwrap();
        let from = store
            .resolve_or_create("telegram", "9876", None)
            .await
            .unwrap();
        assert_ne!(into, from);

        let body = format!(
            r#"{{"into_user_id":"{}","from_user_id":"{}","decided_by":"operator-alice"}}"#,
            into.as_str(),
            from.as_str(),
        );
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/identity/merge")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: MergeOut = serde_json::from_slice(&body).unwrap();
        assert_eq!(v.surviving_user_id, into.as_str());

        // Telegram alias now resolves to `into`.
        let tg_now = store.lookup("telegram", "9876").await.unwrap().unwrap();
        assert_eq!(tg_now, into);
    }

    #[tokio::test]
    async fn merge_returns_404_when_into_missing() {
        let tmp = TempDir::new().unwrap();
        let (state, store) = fresh(&tmp).await;
        let from = store.resolve_or_create("qq", "1234", None).await.unwrap();
        let body = format!(
            r#"{{"into_user_id":"01HV3K9PQRSTUVWXYZABCDEFGH","from_user_id":"{}","decided_by":"operator-alice"}}"#,
            from.as_str(),
        );
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/identity/merge")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn merge_returns_400_for_self_merge() {
        let tmp = TempDir::new().unwrap();
        let (state, store) = fresh(&tmp).await;
        let uid = store.resolve_or_create("qq", "1234", None).await.unwrap();
        let body = format!(
            r#"{{"into_user_id":"{}","from_user_id":"{}","decided_by":"operator-alice"}}"#,
            uid.as_str(),
            uid.as_str(),
        );
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/identity/merge")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn merge_returns_503_when_disabled() {
        let app = router(disabled_state());
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/identity/merge")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        r#"{"into_user_id":"a","from_user_id":"b","decided_by":"x"}"#,
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }
}
