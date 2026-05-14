//! `/admin/federation/peers*` — operator-facing tenant federation
//! peer admin surface (Phase 4 W2 B3 iter 5).
//!
//! Four routes, all behind `require_admin` and `tenant_scope`. They
//! all read or write the `tenant_federation_peers` rows in the root-
//! level `tenants.sqlite` admin DB (`AdminDb`), scoped to the
//! current tenant resolved by the [`Tenant`] extractor:
//!
//! - `GET    /admin/federation/peers`
//!   → `{ accepted_from: FederationPeer[], peers_of_us: FederationPeer[] }`.
//!   `accepted_from` is the recipient view: rows where the current
//!   tenant is the *peer* (i.e. the operator opted to accept from
//!   each `source_tenant_id`). `peers_of_us` is the publishing-side
//!   view: rows where the current tenant is the *source* (other
//!   tenants that accept from us).
//! - `POST   /admin/federation/peers`
//!   body `{ source_tenant_id }` → 201 with the inserted row's
//!   metadata. The current tenant is implicitly the `peer`. Validates
//!   the body slug through [`TenantId::new`]; idempotent at the
//!   admin-DB layer (re-POSTing the same `(peer, source)` pair is a
//!   no-op rather than a 409).
//! - `DELETE /admin/federation/peers/:source_tenant_id`
//!   → `{ removed: bool }`. `removed = false` is a 404 (the spec
//!   distinguishes idempotent revoke from "operator typo'd a slug
//!   that was never actually federated") so the UI can show an
//!   inline error rather than silent success.
//! - `GET    /admin/federation/peers/:source_tenant_id/recent_proposals`
//!   → `{ proposals: [...] }`. Reads the *current tenant*'s
//!   per-tenant `evolution.sqlite` and returns the last 50 federated
//!   proposals received from `source_tenant_id`, filtered on
//!   `metadata.federated_from.tenant = source_tenant_id`. Empty
//!   array when no rows match (or the per-tenant evolution DB
//!   doesn't exist yet — new tenants legitimately hit this path).
//!
//! ### Disabled / not-found paths
//!
//! - **503 `tenants_disabled`** when either `[tenants].enabled =
//!   false` *or* `AdminState::admin_db` is `None`. Same gate as
//!   `/admin/tenants*`; the UI keys off the 503 status to render the
//!   "multi-tenant federation is off" banner. We collapse both
//!   conditions onto one error code because the operator-visible
//!   distinction (config off vs. boot failure) is already surfaced
//!   by the `/admin/tenants` 503 envelope and doesn't need to be
//!   re-emitted on every federation route.
//! - **400 `invalid_tenant_slug`** when the body / path slug fails
//!   the `TenantId::new` regex. UI distinguishes typos from
//!   authorisation failures via the error code.
//! - **404 `not_found`** on DELETE when the `(current_tenant,
//!   source_tenant_id)` pair didn't exist; on the recent_proposals
//!   route this is reserved for "the URL slug itself is malformed"
//!   (an empty proposals array is the legitimate happy-path no-rows
//!   shape, not a 404).
//!
//! ### Wire shape — `recent_proposals`
//!
//! Each row carries its decoded `metadata.federated_from` block so
//! the UI can render source provenance without a second round trip.
//! The `metadata` column is JSON-typed in SQLite; we extract via
//! `json_extract(metadata, '$.federated_from.tenant')` for the
//! WHERE clause and decode the full `federated_from` blob via
//! `serde_json::from_value` after the fetch. Rows where
//! `metadata.federated_from` is missing or malformed are skipped at
//! decode time (defensive — the WHERE clause already filtered by
//! tenant, so a missing field would be a schema mismatch, not a
//! happy-path drop).
//!
//! ### `accepted_by` source
//!
//! Per the iter 5 spec: `accepted_by` is the operator's username
//! from the request session. The admin auth middleware doesn't
//! propagate the resolved username via request extensions today
//! (cookie path validates and short-circuits; Basic-auth path
//! verifies but doesn't stash). Rather than widen that contract for
//! one route, we re-parse the `Authorization: Basic ...` header
//! inline as a best-effort — when present we use the username from
//! the header; otherwise we fall back to `"admin"` (the default
//! configured admin user, matching the convention session-cookie
//! flows already imply). Future iterations can swap this for an
//! `AdminSession` extractor once the middleware grows one.

use std::path::PathBuf;
use std::sync::Arc;

use axum::{
    extract::{Path, State},
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{delete, get},
    Json, Router,
};
use base64::Engine;
use corlinman_evolution::{EvolutionStore, OpenError};
use corlinman_tenant::{tenant_db_path, AdminDb, FederationPeer, TenantId};
use serde::{Deserialize, Serialize};
use serde_json::json;
use tracing::warn;

use super::AdminState;
use crate::middleware::tenant_scope::Tenant;

/* ------------------------------------------------------------------ */
/*                            Wire shapes                              */
/* ------------------------------------------------------------------ */

/// One row of `tenant_federation_peers` projected onto the wire.
/// Mirrors [`FederationPeer`] field-for-field but emits the
/// [`TenantId`] newtype as its bare slug string for the JSON
/// envelope.
#[derive(Debug, Serialize, Deserialize)]
pub struct FederationPeerOut {
    pub peer_tenant_id: String,
    pub source_tenant_id: String,
    pub accepted_at_ms: i64,
    pub accepted_by: Option<String>,
}

impl From<FederationPeer> for FederationPeerOut {
    fn from(p: FederationPeer) -> Self {
        Self {
            peer_tenant_id: p.peer_tenant_id.as_str().to_string(),
            source_tenant_id: p.source_tenant_id.as_str().to_string(),
            accepted_at_ms: p.accepted_at_ms,
            accepted_by: p.accepted_by,
        }
    }
}

/// Wire shape for `GET /admin/federation/peers`. Two perspectives on
/// the same table — the recipient view (`accepted_from`) and the
/// publishing view (`peers_of_us`) — emitted in one round trip so
/// the UI's federation page renders both columns from a single fetch.
#[derive(Debug, Serialize, Deserialize)]
pub struct PeersListOut {
    pub accepted_from: Vec<FederationPeerOut>,
    pub peers_of_us: Vec<FederationPeerOut>,
}

/// Body for `POST /admin/federation/peers`. The current tenant is
/// implicit (resolved by `tenant_scope`); the body says "we accept
/// from this source".
#[derive(Debug, Deserialize)]
pub struct AddPeerBody {
    pub source_tenant_id: String,
}

/// Wire shape for the `POST /admin/federation/peers` 201 response.
/// Echoes the inserted row's full metadata so the UI can render the
/// new entry without a follow-up GET.
#[derive(Debug, Serialize, Deserialize)]
pub struct AddPeerOut {
    pub peer_tenant_id: String,
    pub source_tenant_id: String,
    pub accepted_at_ms: i64,
    pub accepted_by: String,
}

/// Wire shape for `DELETE /admin/federation/peers/:source_tenant_id`
/// 200 response. `removed = true` always when the response is 200;
/// the route returns 404 rather than a 200-with-`removed:false` when
/// the row didn't exist, so the UI can distinguish the two.
#[derive(Debug, Serialize, Deserialize)]
pub struct RemovePeerOut {
    pub removed: bool,
}

/// One row in the `recent_proposals` response. Carries the full
/// `metadata.federated_from` block decoded into a typed struct so
/// the UI's detail panel can render provenance without re-decoding
/// the JSON blob on the client.
#[derive(Debug, Serialize, Deserialize)]
pub struct FederatedProposalOut {
    pub id: String,
    pub kind: String,
    pub status: String,
    pub created_at: i64,
    pub federated_from: FederatedFromOut,
}

/// Decoded `metadata.federated_from` block. Mirrors the
/// `FederationLink` shape from the design doc: `{ tenant,
/// source_proposal_id, hop }`.
#[derive(Debug, Serialize, Deserialize)]
pub struct FederatedFromOut {
    pub tenant: String,
    pub source_proposal_id: String,
    pub hop: u8,
}

/// Wire shape for `GET /admin/federation/peers/:source_tenant_id/
/// recent_proposals`. Empty array when no rows match — the UI's
/// detail panel renders "no federated proposals yet" rather than
/// surfacing a 404, since the source slug being valid + opted-in
/// doesn't imply at least one proposal has crossed yet.
#[derive(Debug, Serialize, Deserialize)]
pub struct RecentProposalsOut {
    pub proposals: Vec<FederatedProposalOut>,
}

/* ------------------------------------------------------------------ */
/*                          Router + helpers                           */
/* ------------------------------------------------------------------ */

/// Sub-router for `/admin/federation/peers*`. Mounted by
/// [`super::router_with_state`] inside both `require_admin` and
/// `tenant_scope`, so every handler observes a resolved current
/// tenant via the [`Tenant`] extractor.
pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/admin/federation/peers", get(list_peers).post(add_peer))
        .route(
            "/admin/federation/peers/:source_tenant_id",
            delete(remove_peer),
        )
        .route(
            "/admin/federation/peers/:source_tenant_id/recent_proposals",
            get(recent_proposals),
        )
        .with_state(state)
}

fn tenants_disabled_503() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({
            "error": "tenants_disabled",
        })),
    )
        .into_response()
}

fn invalid_tenant_slug(slug: &str, reason: impl Into<String>) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({
            "error": "invalid_tenant_slug",
            "slug": slug,
            "reason": reason.into(),
        })),
    )
        .into_response()
}

fn peer_not_found(source: &str) -> Response {
    (
        StatusCode::NOT_FOUND,
        Json(json!({
            "error": "not_found",
            "source_tenant_id": source,
        })),
    )
        .into_response()
}

fn storage_error(err: impl std::fmt::Display, ctx: &'static str) -> Response {
    warn!(error = %err, "admin/federation {ctx} failed");
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        Json(json!({
            "error": "storage_error",
            "message": err.to_string(),
        })),
    )
        .into_response()
}

/// Resolve the data directory exactly the way `sessions.rs::resolve_data_dir`
/// does — prefer the explicit override on `AdminState` (tests pin it to a
/// tempdir) and fall back to `CORLINMAN_DATA_DIR` → `~/.corlinman`. Local
/// helper so the federation route doesn't pull a dependency on a
/// `super`-level helper that isn't currently exposed.
fn resolve_data_dir(state: &AdminState) -> PathBuf {
    if let Some(p) = state.data_dir.as_ref() {
        return p.clone();
    }
    if let Ok(dir) = std::env::var("CORLINMAN_DATA_DIR") {
        return PathBuf::from(dir);
    }
    dirs::home_dir()
        .map(|h| h.join(".corlinman"))
        .unwrap_or_else(|| PathBuf::from(".corlinman"))
}

/// Decide which "disabled" envelope applies. Returns `Some(response)`
/// when the route should short-circuit and `None` when handlers can
/// proceed. Mirrors the `/admin/tenants` gate but collapses
/// "config-off" and "admin DB missing" onto the same 503 envelope —
/// see module docs.
fn require_admin_db(state: &AdminState) -> Result<Arc<AdminDb>, Box<Response>> {
    let cfg = state.config.load();
    if !cfg.tenants.enabled {
        return Err(Box::new(tenants_disabled_503()));
    }
    state
        .admin_db
        .clone()
        .ok_or_else(|| Box::new(tenants_disabled_503()))
}

/// Best-effort extraction of the operator's username from the
/// request. Tries the `Authorization: Basic ...` header first; falls
/// back to `"admin"` when no Basic header is present (cookie-only
/// flows). The middleware short-circuits unauthenticated requests
/// upstream of this handler, so the fallback only fires on the
/// happy-path session-cookie case where we genuinely don't have the
/// username at this layer.
fn admin_username(headers: &HeaderMap) -> String {
    if let Some(auth) = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
    {
        if let Some(rest) = auth.strip_prefix("Basic ") {
            if let Ok(decoded) = base64::engine::general_purpose::STANDARD.decode(rest.trim()) {
                if let Ok(s) = String::from_utf8(decoded) {
                    if let Some((user, _)) = s.split_once(':') {
                        if !user.is_empty() {
                            return user.to_string();
                        }
                    }
                }
            }
        }
    }
    "admin".to_string()
}

/* ------------------------------------------------------------------ */
/*                              Handlers                                */
/* ------------------------------------------------------------------ */

async fn list_peers(State(state): State<AdminState>, Tenant(tenant): Tenant) -> Response {
    let db = match require_admin_db(&state) {
        Ok(d) => d,
        Err(resp) => return *resp,
    };

    let accepted_from = match db.list_federation_sources_for(&tenant).await {
        Ok(rows) => rows,
        Err(err) => return storage_error(err, "list_sources_for"),
    };
    let peers_of_us = match db.list_federation_peers_of(&tenant).await {
        Ok(rows) => rows,
        Err(err) => return storage_error(err, "list_peers_of"),
    };

    let out = PeersListOut {
        accepted_from: accepted_from.into_iter().map(Into::into).collect(),
        peers_of_us: peers_of_us.into_iter().map(Into::into).collect(),
    };
    (StatusCode::OK, Json(out)).into_response()
}

async fn add_peer(
    State(state): State<AdminState>,
    Tenant(tenant): Tenant,
    headers: HeaderMap,
    body: Option<Json<AddPeerBody>>,
) -> Response {
    let db = match require_admin_db(&state) {
        Ok(d) => d,
        Err(resp) => return *resp,
    };
    let Some(Json(body)) = body else {
        return invalid_tenant_slug("", "body must include source_tenant_id");
    };
    let source = match TenantId::new(body.source_tenant_id.clone()) {
        Ok(t) => t,
        Err(err) => return invalid_tenant_slug(&body.source_tenant_id, err.to_string()),
    };
    if source == tenant {
        // The DB-layer `INSERT OR IGNORE` would silently no-op on a
        // self-peer because `(peer, source)` would collide, but
        // self-peering is a logical operator error rather than an
        // idempotent re-add — flag it as a 400 so the UI can render
        // a helpful "you can't federate with yourself" hint.
        return invalid_tenant_slug(
            tenant.as_str(),
            "self-peering is not allowed (source must differ from current tenant)",
        );
    }

    let accepted_by = admin_username(&headers);

    if let Err(err) = db.add_federation_peer(&tenant, &source, &accepted_by).await {
        return storage_error(err, "add_federation_peer");
    }

    // Fetch back the actual row so the response carries the *real*
    // stored timestamp and `accepted_by`. Idempotent re-adds preserve
    // the original stamp / user (per the AdminDb contract), so this
    // also disambiguates "fresh insert" from "no-op idempotent".
    let stored = match db.list_federation_sources_for(&tenant).await {
        Ok(rows) => rows.into_iter().find(|r| r.source_tenant_id == source),
        Err(err) => return storage_error(err, "add_federation_peer.readback"),
    };
    let Some(stored) = stored else {
        // Should not happen — we just inserted with INSERT OR IGNORE
        // and the read happens against the same pool. Surface as a
        // storage_error rather than panic.
        return storage_error(
            "readback found no row after add",
            "add_federation_peer.readback",
        );
    };

    (
        StatusCode::CREATED,
        Json(AddPeerOut {
            peer_tenant_id: tenant.as_str().to_string(),
            source_tenant_id: source.as_str().to_string(),
            accepted_at_ms: stored.accepted_at_ms,
            accepted_by: stored.accepted_by.unwrap_or(accepted_by),
        }),
    )
        .into_response()
}

async fn remove_peer(
    State(state): State<AdminState>,
    Tenant(tenant): Tenant,
    Path(source_raw): Path<String>,
) -> Response {
    let db = match require_admin_db(&state) {
        Ok(d) => d,
        Err(resp) => return *resp,
    };
    let source = match TenantId::new(source_raw.clone()) {
        Ok(t) => t,
        Err(err) => return invalid_tenant_slug(&source_raw, err.to_string()),
    };

    match db.remove_federation_peer(&tenant, &source).await {
        Ok(true) => (StatusCode::OK, Json(RemovePeerOut { removed: true })).into_response(),
        Ok(false) => peer_not_found(&source_raw),
        Err(err) => storage_error(err, "remove_federation_peer"),
    }
}

async fn recent_proposals(
    State(state): State<AdminState>,
    Tenant(tenant): Tenant,
    Path(source_raw): Path<String>,
) -> Response {
    let _db = match require_admin_db(&state) {
        Ok(d) => d,
        Err(resp) => return *resp,
    };

    let source = match TenantId::new(source_raw.clone()) {
        Ok(t) => t,
        Err(err) => return invalid_tenant_slug(&source_raw, err.to_string()),
    };

    // Open the *current tenant*'s evolution.sqlite to read the
    // federated rows it has received from `source`. Per-tenant DB
    // path follows the same `<data_dir>/tenants/<tenant>/<name>.sqlite`
    // layout the rest of Phase 4 W1 uses. Open is per-request because
    // the federation route doesn't sit on the `EvolutionApplier`'s
    // hot path; the cost is amortised by SQLite's connection cache.
    let data_dir = resolve_data_dir(&state);
    let evo_path = tenant_db_path(&data_dir, &tenant, "evolution");

    if !evo_path.exists() {
        // New tenant that hasn't received any federated proposals
        // yet — return the empty-list happy path rather than 503.
        return (
            StatusCode::OK,
            Json(RecentProposalsOut { proposals: vec![] }),
        )
            .into_response();
    }

    let store = match EvolutionStore::open(&evo_path).await {
        Ok(s) => s,
        Err(OpenError::Connect(_, e)) | Err(OpenError::ApplySchema(e)) => {
            return storage_error(e, "open_evolution");
        }
        Err(err) => return storage_error(err, "open_evolution"),
    };

    // Pull the last 50 proposals whose `metadata.federated_from.tenant`
    // matches `source`. SQLite's `json_extract` returns NULL for
    // missing keys, so the `= ?` predicate naturally filters out rows
    // without a `federated_from` block. We project the columns we
    // need (id/kind/status/created_at) plus the raw metadata JSON so
    // we can decode the full `federated_from` block in Rust without
    // adding three more SQL projections.
    let rows = match sqlx::query(
        r#"SELECT id, kind, status, created_at, metadata
           FROM evolution_proposals
           WHERE json_extract(metadata, '$.federated_from.tenant') = ?
           ORDER BY created_at DESC
           LIMIT 50"#,
    )
    .bind(source.as_str())
    .fetch_all(store.pool())
    .await
    {
        Ok(rows) => rows,
        Err(err) => return storage_error(err, "query_recent_proposals"),
    };

    let mut proposals: Vec<FederatedProposalOut> = Vec::with_capacity(rows.len());
    for row in rows {
        use sqlx::Row;
        let id: String = row.get("id");
        let kind: String = row.get("kind");
        let status: String = row.get("status");
        let created_at: i64 = row.get("created_at");
        // `metadata` is TEXT NULL; the WHERE clause filtered to rows
        // where `$.federated_from.tenant` is non-NULL, so a malformed
        // or null blob here is a schema-mismatch we surface as a
        // skip-with-warn rather than a 500 — the route remains
        // available even if one row's metadata got corrupted.
        let raw_meta: Option<String> = row.get("metadata");
        let Some(raw_meta) = raw_meta else {
            warn!(id = %id, "federated proposal row has NULL metadata despite WHERE filter");
            continue;
        };
        let blob: serde_json::Value = match serde_json::from_str(&raw_meta) {
            Ok(v) => v,
            Err(err) => {
                warn!(id = %id, error = %err, "federated proposal metadata not valid JSON");
                continue;
            }
        };
        let Some(fed_from) = blob.get("federated_from") else {
            warn!(id = %id, "federated proposal metadata missing federated_from despite WHERE filter");
            continue;
        };
        let federated_from: FederatedFromOut = match serde_json::from_value(fed_from.clone()) {
            Ok(v) => v,
            Err(err) => {
                warn!(id = %id, error = %err, "federated_from blob shape mismatch");
                continue;
            }
        };
        proposals.push(FederatedProposalOut {
            id,
            kind,
            status,
            created_at,
            federated_from,
        });
    }

    (StatusCode::OK, Json(RecentProposalsOut { proposals })).into_response()
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
    use corlinman_evolution::{
        EvolutionKind, EvolutionProposal, EvolutionRisk, EvolutionStatus, ProposalId, ProposalsRepo,
    };
    use corlinman_plugins::registry::PluginRegistry;
    use corlinman_tenant::{tenant_db_path, AdminDb, TenantId};
    use std::sync::Arc;
    use tempfile::TempDir;
    use tower::ServiceExt;

    /// Build a fresh `AdminState` with `[tenants].enabled = true`, a
    /// tempdir-backed `AdminDb`, and `data_dir` pinned to the tempdir
    /// so the recent_proposals route reads from
    /// `<tmp>/tenants/<slug>/evolution.sqlite`.
    async fn fresh(tmp: &TempDir) -> (AdminState, Arc<AdminDb>) {
        let mut cfg = Config::default();
        cfg.tenants.enabled = true;
        let db = Arc::new(
            AdminDb::open(&tmp.path().join("tenants.sqlite"))
                .await
                .expect("AdminDb::open"),
        );
        let state = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        )
        .with_admin_db(db.clone())
        .with_data_dir(tmp.path().to_path_buf());
        (state, db)
    }

    /// Build an `AdminState` whose `admin_db` is None — drives the
    /// 503 disabled path. Config can have `tenants.enabled = true`
    /// or `false`; both must collapse to 503.
    fn disabled_state(enabled: bool) -> AdminState {
        let mut cfg = Config::default();
        cfg.tenants.enabled = enabled;
        AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        )
    }

    fn req(method: &str, uri: &str, tenant: TenantId, body: Body) -> Request<Body> {
        Request::builder()
            .method(method)
            .uri(uri)
            .extension(tenant)
            .header("content-type", "application/json")
            .body(body)
            .unwrap()
    }

    /* -------------------------- list happy path -------------------------- */

    #[tokio::test]
    async fn list_returns_accepted_from_and_peers_of_us() {
        let tmp = TempDir::new().unwrap();
        let (state, db) = fresh(&tmp).await;
        let acme = TenantId::new("acme").unwrap();
        let bravo = TenantId::new("bravo").unwrap();
        let charlie = TenantId::new("charlie").unwrap();

        // acme accepts from bravo, charlie accepts from acme.
        db.add_federation_peer(&acme, &bravo, "alice")
            .await
            .unwrap();
        db.add_federation_peer(&charlie, &acme, "bob")
            .await
            .unwrap();

        let app = router(state);
        let resp = app
            .oneshot(req(
                "GET",
                "/admin/federation/peers",
                acme.clone(),
                Body::empty(),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: PeersListOut = serde_json::from_slice(&body).unwrap();

        assert_eq!(v.accepted_from.len(), 1);
        assert_eq!(v.accepted_from[0].source_tenant_id, "bravo");
        assert_eq!(v.accepted_from[0].peer_tenant_id, "acme");
        assert_eq!(v.accepted_from[0].accepted_by.as_deref(), Some("alice"));

        assert_eq!(v.peers_of_us.len(), 1);
        assert_eq!(v.peers_of_us[0].peer_tenant_id, "charlie");
        assert_eq!(v.peers_of_us[0].source_tenant_id, "acme");
    }

    #[tokio::test]
    async fn list_returns_503_when_admin_db_missing() {
        let app = router(disabled_state(true));
        let resp = app
            .oneshot(req(
                "GET",
                "/admin/federation/peers",
                TenantId::legacy_default(),
                Body::empty(),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["error"], "tenants_disabled");
    }

    #[tokio::test]
    async fn list_returns_503_when_tenants_disabled() {
        // tenants.enabled = false but admin_db present (via a fresh
        // state fixture) is still 503 because the config-level gate
        // takes precedence.
        let tmp = TempDir::new().unwrap();
        let (mut state, _db) = fresh(&tmp).await;
        let mut cfg = (**state.config.load()).clone();
        cfg.tenants.enabled = false;
        state.config = Arc::new(ArcSwap::from_pointee(cfg));
        let app = router(state);
        let resp = app
            .oneshot(req(
                "GET",
                "/admin/federation/peers",
                TenantId::legacy_default(),
                Body::empty(),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    /* -------------------------- add happy path --------------------------- */

    #[tokio::test]
    async fn add_creates_row_and_returns_201() {
        let tmp = TempDir::new().unwrap();
        let (state, db) = fresh(&tmp).await;
        let acme = TenantId::new("acme").unwrap();

        let app = router(state);
        let resp = app
            .oneshot(req(
                "POST",
                "/admin/federation/peers",
                acme.clone(),
                Body::from(r#"{"source_tenant_id":"bravo"}"#),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::CREATED);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: AddPeerOut = serde_json::from_slice(&body).unwrap();
        assert_eq!(v.peer_tenant_id, "acme");
        assert_eq!(v.source_tenant_id, "bravo");
        assert_eq!(v.accepted_by, "admin"); // no Basic header → fallback
        assert!(v.accepted_at_ms > 1_000_000_000_000);

        // Sanity: row actually landed in the admin DB.
        let bravo = TenantId::new("bravo").unwrap();
        let rows = db.list_federation_sources_for(&acme).await.unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].source_tenant_id, bravo);
    }

    #[tokio::test]
    async fn add_records_username_from_basic_auth_header() {
        let tmp = TempDir::new().unwrap();
        let (state, db) = fresh(&tmp).await;
        let acme = TenantId::new("acme").unwrap();
        let app = router(state);

        let basic = format!(
            "Basic {}",
            base64::engine::general_purpose::STANDARD.encode("alice-the-operator:irrelevant")
        );
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/federation/peers")
                    .extension(acme.clone())
                    .header("content-type", "application/json")
                    .header(header::AUTHORIZATION, basic)
                    .body(Body::from(r#"{"source_tenant_id":"bravo"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::CREATED);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: AddPeerOut = serde_json::from_slice(&body).unwrap();
        assert_eq!(v.accepted_by, "alice-the-operator");

        let rows = db.list_federation_sources_for(&acme).await.unwrap();
        assert_eq!(rows[0].accepted_by.as_deref(), Some("alice-the-operator"));
    }

    #[tokio::test]
    async fn add_returns_400_for_invalid_slug() {
        let tmp = TempDir::new().unwrap();
        let (state, _db) = fresh(&tmp).await;
        let acme = TenantId::new("acme").unwrap();

        let app = router(state);
        let resp = app
            .oneshot(req(
                "POST",
                "/admin/federation/peers",
                acme,
                Body::from(r#"{"source_tenant_id":"NOT a slug"}"#),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["error"], "invalid_tenant_slug");
    }

    #[tokio::test]
    async fn add_returns_400_for_self_peer() {
        let tmp = TempDir::new().unwrap();
        let (state, _db) = fresh(&tmp).await;
        let acme = TenantId::new("acme").unwrap();

        let app = router(state);
        let resp = app
            .oneshot(req(
                "POST",
                "/admin/federation/peers",
                acme,
                Body::from(r#"{"source_tenant_id":"acme"}"#),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn add_returns_503_when_admin_db_missing() {
        let app = router(disabled_state(true));
        let resp = app
            .oneshot(req(
                "POST",
                "/admin/federation/peers",
                TenantId::legacy_default(),
                Body::from(r#"{"source_tenant_id":"bravo"}"#),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    /* -------------------------- delete happy path ------------------------ */

    #[tokio::test]
    async fn delete_removes_existing_pair() {
        let tmp = TempDir::new().unwrap();
        let (state, db) = fresh(&tmp).await;
        let acme = TenantId::new("acme").unwrap();
        let bravo = TenantId::new("bravo").unwrap();
        db.add_federation_peer(&acme, &bravo, "alice")
            .await
            .unwrap();

        let app = router(state);
        let resp = app
            .oneshot(req(
                "DELETE",
                "/admin/federation/peers/bravo",
                acme.clone(),
                Body::empty(),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: RemovePeerOut = serde_json::from_slice(&body).unwrap();
        assert!(v.removed);

        let rows = db.list_federation_sources_for(&acme).await.unwrap();
        assert!(rows.is_empty());
    }

    #[tokio::test]
    async fn delete_returns_404_for_nonexistent_pair() {
        let tmp = TempDir::new().unwrap();
        let (state, _db) = fresh(&tmp).await;
        let acme = TenantId::new("acme").unwrap();

        let app = router(state);
        let resp = app
            .oneshot(req(
                "DELETE",
                "/admin/federation/peers/never-added",
                acme,
                Body::empty(),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["error"], "not_found");
        assert_eq!(v["source_tenant_id"], "never-added");
    }

    #[tokio::test]
    async fn delete_returns_503_when_admin_db_missing() {
        let app = router(disabled_state(true));
        let resp = app
            .oneshot(req(
                "DELETE",
                "/admin/federation/peers/bravo",
                TenantId::legacy_default(),
                Body::empty(),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    /* ----------------------- recent_proposals path ----------------------- */

    /// Helper: open the per-tenant evolution.sqlite at the layout
    /// the route expects, insert a single proposal whose `metadata`
    /// blob carries the supplied `federated_from` shape.
    async fn seed_federated_proposal(
        tmp: &TempDir,
        tenant: &TenantId,
        proposal_id: &str,
        meta: serde_json::Value,
    ) {
        let path = tenant_db_path(tmp.path(), tenant, "evolution");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let store = EvolutionStore::open(&path).await.unwrap();
        let repo = ProposalsRepo::new(store.pool().clone());
        repo.insert(&EvolutionProposal {
            id: ProposalId::new(proposal_id),
            kind: EvolutionKind::SkillUpdate,
            target: "skills/web_search".into(),
            diff: "+ a line".into(),
            reasoning: "[federated from acme] real fix".into(),
            risk: EvolutionRisk::Low,
            budget_cost: 0,
            status: EvolutionStatus::Pending,
            shadow_metrics: None,
            signal_ids: vec![],
            trace_ids: vec![],
            created_at: 1_777_000_000_000,
            decided_at: None,
            decided_by: None,
            applied_at: None,
            rollback_of: None,
            eval_run_id: None,
            baseline_metrics_json: None,
            auto_rollback_at: None,
            auto_rollback_reason: None,
            metadata: Some(meta),
        })
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn recent_proposals_returns_rows_filtered_by_source() {
        let tmp = TempDir::new().unwrap();
        let (state, _db) = fresh(&tmp).await;
        let bravo = TenantId::new("bravo").unwrap();

        // Seed bravo's evolution.sqlite with two federated rows: one
        // from acme (we want this), one from charlie (filtered out).
        seed_federated_proposal(
            &tmp,
            &bravo,
            "evol-from-acme-1",
            json!({
                "federated_from": {
                    "tenant": "acme",
                    "source_proposal_id": "evol-acme-2026-05-01-007",
                    "hop": 1,
                }
            }),
        )
        .await;
        seed_federated_proposal(
            &tmp,
            &bravo,
            "evol-from-charlie-1",
            json!({
                "federated_from": {
                    "tenant": "charlie",
                    "source_proposal_id": "evol-charlie-2026-05-02-001",
                    "hop": 1,
                }
            }),
        )
        .await;

        let app = router(state);
        let resp = app
            .oneshot(req(
                "GET",
                "/admin/federation/peers/acme/recent_proposals",
                bravo.clone(),
                Body::empty(),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: RecentProposalsOut = serde_json::from_slice(&body).unwrap();

        assert_eq!(v.proposals.len(), 1, "only the acme-sourced row");
        assert_eq!(v.proposals[0].id, "evol-from-acme-1");
        assert_eq!(v.proposals[0].kind, "skill_update");
        assert_eq!(v.proposals[0].status, "pending");
        assert_eq!(v.proposals[0].federated_from.tenant, "acme");
        assert_eq!(
            v.proposals[0].federated_from.source_proposal_id,
            "evol-acme-2026-05-01-007"
        );
        assert_eq!(v.proposals[0].federated_from.hop, 1);
    }

    #[tokio::test]
    async fn recent_proposals_returns_empty_array_when_no_db() {
        // Brand-new tenant whose per-tenant evolution.sqlite hasn't
        // been opened yet — recent_proposals must still be 200 with
        // an empty list, not a 500/503.
        let tmp = TempDir::new().unwrap();
        let (state, _db) = fresh(&tmp).await;
        let acme = TenantId::new("acme").unwrap();

        let app = router(state);
        let resp = app
            .oneshot(req(
                "GET",
                "/admin/federation/peers/bravo/recent_proposals",
                acme,
                Body::empty(),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: RecentProposalsOut = serde_json::from_slice(&body).unwrap();
        assert!(v.proposals.is_empty());
    }

    #[tokio::test]
    async fn recent_proposals_returns_400_for_invalid_slug() {
        let tmp = TempDir::new().unwrap();
        let (state, _db) = fresh(&tmp).await;
        let acme = TenantId::new("acme").unwrap();

        let app = router(state);
        let resp = app
            .oneshot(req(
                "GET",
                "/admin/federation/peers/NOT-A-SLUG/recent_proposals",
                acme,
                Body::empty(),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn recent_proposals_returns_503_when_admin_db_missing() {
        let app = router(disabled_state(true));
        let resp = app
            .oneshot(req(
                "GET",
                "/admin/federation/peers/bravo/recent_proposals",
                TenantId::legacy_default(),
                Body::empty(),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    /* ------------------------- cross-tenant scoping ---------------------- */

    #[tokio::test]
    async fn accepted_from_is_per_tenant_self_managed() {
        // The recipient view (`accepted_from`) must surface only the
        // *current* tenant's opt-ins, not anybody else's. Seed two
        // tenants; assert acme's list only sees what acme accepted.
        let tmp = TempDir::new().unwrap();
        let (state, db) = fresh(&tmp).await;
        let acme = TenantId::new("acme").unwrap();
        let bravo = TenantId::new("bravo").unwrap();
        let charlie = TenantId::new("charlie").unwrap();

        db.add_federation_peer(&acme, &bravo, "alice")
            .await
            .unwrap();
        // bravo accepts from charlie — must NOT show up in acme's list.
        db.add_federation_peer(&bravo, &charlie, "bob")
            .await
            .unwrap();

        let app = router(state);
        let resp = app
            .oneshot(req(
                "GET",
                "/admin/federation/peers",
                acme.clone(),
                Body::empty(),
            ))
            .await
            .unwrap();
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: PeersListOut = serde_json::from_slice(&body).unwrap();
        assert_eq!(v.accepted_from.len(), 1);
        assert_eq!(v.accepted_from[0].source_tenant_id, "bravo");
        // bravo's accepted_from row (charlie) must not bleed in.
        assert!(
            v.accepted_from
                .iter()
                .all(|r| r.source_tenant_id != "charlie"),
            "acme must not see bravo's opt-in to charlie"
        );
    }

    #[tokio::test]
    async fn cross_tenant_lists_do_not_bleed() {
        // Strong form of the cross-tenant test: explicitly resolve
        // both tenants in turn through the same router and assert
        // the lists never contain rows owned by the other tenant.
        let tmp = TempDir::new().unwrap();
        let (state, db) = fresh(&tmp).await;
        let a = TenantId::new("alpha").unwrap();
        let b = TenantId::new("bravo").unwrap();
        let c = TenantId::new("charlie").unwrap();
        let d = TenantId::new("delta").unwrap();

        // alpha accepts from bravo; charlie accepts from delta.
        db.add_federation_peer(&a, &b, "op-alpha").await.unwrap();
        db.add_federation_peer(&c, &d, "op-charlie").await.unwrap();

        let app = router(state);

        // alpha sees only bravo.
        let resp_a = app
            .clone()
            .oneshot(req("GET", "/admin/federation/peers", a, Body::empty()))
            .await
            .unwrap();
        let body_a = to_bytes(resp_a.into_body(), usize::MAX).await.unwrap();
        let v_a: PeersListOut = serde_json::from_slice(&body_a).unwrap();
        assert_eq!(v_a.accepted_from.len(), 1);
        assert_eq!(v_a.accepted_from[0].source_tenant_id, "bravo");

        // charlie sees only delta.
        let resp_c = app
            .oneshot(req("GET", "/admin/federation/peers", c, Body::empty()))
            .await
            .unwrap();
        let body_c = to_bytes(resp_c.into_body(), usize::MAX).await.unwrap();
        let v_c: PeersListOut = serde_json::from_slice(&body_c).unwrap();
        assert_eq!(v_c.accepted_from.len(), 1);
        assert_eq!(v_c.accepted_from[0].source_tenant_id, "delta");
    }
}
