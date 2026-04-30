//! Phase 4 W1 4-1B: end-to-end exercise of the `/admin/tenants*` admin
//! routes against a tempdir-backed `AdminDb`.
//!
//! The point isn't to bind a network listener — `tower::ServiceExt::oneshot`
//! drives the axum `Router` in-process — it's to prove the wire-up between
//! `AdminDb`, `AdminState`, the sub-router, and the typed JSON envelope is
//! consistent and matches the UI mock contract pinned in
//! `ui/lib/api/tenants.ts` + `ui/mock/server.ts`.
//!
//! Test matrix (mirrors the acceptance bullets in the agent prompt):
//!
//! 1. `list_returns_tenants_and_allowed`     — happy path GET with seeded rows
//! 2. `list_403_when_tenants_disabled`       — config-level disable
//! 3. `list_503_when_admin_db_missing`       — boot-time open failed
//! 4. `create_201_writes_db_and_dirs`        — happy path POST
//! 5. `create_400_invalid_slug`              — slug fails TenantId regex
//! 6. `create_400_missing_admin_username`    — empty admin_username
//! 7. `create_400_missing_admin_password`    — empty admin_password
//! 8. `create_409_duplicate`                 — slug already in tenants.sqlite
//! 9. `create_403_when_tenants_disabled`     — POST mirrors GET disabled path
//!
//! Each test stands up its own `TempDir` + `AdminDb` so they're independent.
//! `CORLINMAN_DATA_DIR` is set to the tempdir so `create_tenant`'s dir-tree
//! creation lands inside it; std::env is process-global, so multi-tenant
//! tests must `serial_test`-style guard against each other — we don't, and
//! instead each test uses its own tempdir + reads back the dir tree at *that*
//! tempdir to assert. The env-var-set is overwritten each test, which is
//! sequential enough for the matrix here (cargo test default runs each
//! `#[tokio::test]` on its own worker, but they share the process).

use std::sync::{Arc, OnceLock};

use arc_swap::ArcSwap;
use axum::body::{to_bytes, Body};
use axum::http::{Request, StatusCode};
use corlinman_core::config::Config;
use corlinman_gateway::routes::admin::{tenants as tenants_routes, AdminState};
use corlinman_plugins::registry::PluginRegistry;
use corlinman_tenant::{AdminDb, TenantId};
use serde_json::Value;
use tempfile::TempDir;
use tokio::sync::{Mutex, MutexGuard};
use tower::ServiceExt;

/// Mutex serialising tests that write to `CORLINMAN_DATA_DIR`. Cargo runs
/// `#[tokio::test]`s in the same binary in parallel by default, and the
/// env var is process-global; without this guard the `create_*` tests
/// race each other and the side effect (per-tenant dir creation) lands
/// in whichever tempdir the most recent setter pointed at, breaking
/// dir-existence asserts. We use `tokio::sync::Mutex` rather than
/// `std::sync::Mutex` so the guard can be held across `.await` points
/// without tripping clippy's `await_holding_lock` lint.
fn data_dir_lock() -> &'static Mutex<()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
}

/// Build an `AdminState` with `[tenants].enabled = true`, an `AdminDb`
/// opened at `<tmp>/tenants.sqlite`, and `CORLINMAN_DATA_DIR` pointed
/// at the tempdir so `create_tenant` lands its dir-tree there.
///
/// Returns the admin state + the tempdir + the data-dir-lock guard.
/// Callers hold the guard for the full test body so concurrent
/// `#[tokio::test]`s don't race on the global env var.
async fn state_enabled() -> (AdminState, TempDir, MutexGuard<'static, ()>) {
    let guard = data_dir_lock().lock().await;
    let tmp = TempDir::new().expect("tempdir");
    std::env::set_var("CORLINMAN_DATA_DIR", tmp.path());

    let mut cfg = Config::default();
    cfg.tenants.enabled = true;

    let mut allowed = std::collections::BTreeSet::new();
    allowed.insert(TenantId::legacy_default());

    let db = AdminDb::open(&tmp.path().join("tenants.sqlite"))
        .await
        .expect("AdminDb::open");

    let state = AdminState::new(
        Arc::new(PluginRegistry::default()),
        Arc::new(ArcSwap::from_pointee(cfg)),
    )
    .with_admin_db(Arc::new(db.clone()))
    .with_allowed_tenants(allowed);

    (state, tmp, guard)
}

async fn body_json(res: axum::response::Response) -> Value {
    let bytes = to_bytes(res.into_body(), usize::MAX)
        .await
        .expect("read body");
    serde_json::from_slice(&bytes).expect("decode body")
}

#[tokio::test]
async fn list_returns_tenants_and_allowed() {
    let (state, _tmp, _guard) = state_enabled().await;

    // Seed two rows directly via the AdminDb on the state.
    let db = state.admin_db.as_ref().expect("admin_db wired").clone();
    let acme = TenantId::new("acme").unwrap();
    let bravo = TenantId::new("bravo").unwrap();
    db.create_tenant(&acme, "Acme Corp", 1_777_593_600_000)
        .await
        .unwrap();
    db.create_tenant(&bravo, "Bravo Inc", 1_777_593_700_000)
        .await
        .unwrap();

    let app = tenants_routes::router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/admin/tenants")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let body = body_json(resp).await;
    let tenants = body["tenants"].as_array().expect("tenants array");
    assert_eq!(tenants.len(), 2, "expected two seeded rows");
    assert_eq!(tenants[0]["tenant_id"], "acme");
    assert_eq!(tenants[0]["display_name"], "Acme Corp");
    // RFC-3339 / ISO-8601 conversion from unix-millis.
    assert!(
        tenants[0]["created_at"]
            .as_str()
            .unwrap()
            .starts_with("2026-05-01T"),
        "created_at should be ISO-8601: {:?}",
        tenants[0]["created_at"]
    );
    assert_eq!(tenants[1]["tenant_id"], "bravo");

    let allowed = body["allowed"].as_array().expect("allowed array");
    // The fixture pre-loaded only `default` into the allow-set.
    assert!(allowed.iter().any(|v| v == "default"));
}

#[tokio::test]
async fn list_403_when_tenants_disabled() {
    // [tenants].enabled = false (the Config::default()).
    let cfg = Config::default();
    let state = AdminState::new(
        Arc::new(PluginRegistry::default()),
        Arc::new(ArcSwap::from_pointee(cfg)),
    );
    let app = tenants_routes::router(state);
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
async fn list_503_when_admin_db_missing() {
    let mut cfg = Config::default();
    cfg.tenants.enabled = true;
    // No `with_admin_db(...)` — boot-time open failed.
    let state = AdminState::new(
        Arc::new(PluginRegistry::default()),
        Arc::new(ArcSwap::from_pointee(cfg)),
    );
    let app = tenants_routes::router(state);
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

#[tokio::test]
async fn create_201_writes_db_and_dirs() {
    let (state, tmp, _guard) = state_enabled().await;
    let db = state.admin_db.as_ref().expect("admin_db wired").clone();

    // Read CORLINMAN_DATA_DIR back at the moment we issue the request:
    // env vars are process-global and another `#[tokio::test]` in this
    // binary may have overwritten our `state_enabled()` set. The handler
    // resolves the data dir via the env var at request time, so what
    // matters is what's in the var when the handler runs — not whatever
    // value our `tmp` variable holds.
    let data_dir = std::env::var("CORLINMAN_DATA_DIR")
        .map(std::path::PathBuf::from)
        .expect("CORLINMAN_DATA_DIR was set in state_enabled()");

    let app = tenants_routes::router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/admin/tenants")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{
                        "slug": "acme",
                        "display_name": "Acme Corp",
                        "admin_username": "alice",
                        "admin_password": "not-a-secret"
                    }"#,
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);
    let body = body_json(resp).await;
    assert_eq!(body["tenant_id"], "acme");

    // Tenant row landed in `tenants.sqlite`.
    let row = db
        .get(&TenantId::new("acme").unwrap())
        .await
        .unwrap()
        .expect("row exists");
    assert_eq!(row.display_name, "Acme Corp");

    // Admin row landed too, with an argon2id hash (not the plaintext).
    let admins = db
        .list_admins(&TenantId::new("acme").unwrap())
        .await
        .unwrap();
    assert_eq!(admins.len(), 1);
    assert_eq!(admins[0].username, "alice");
    assert!(admins[0].password_hash.starts_with("$argon2id$"));
    assert!(!admins[0].password_hash.contains("not-a-secret"));

    // Per-tenant directory tree exists under <data_dir>/tenants/<slug>/.
    // We deliberately read CORLINMAN_DATA_DIR rather than using `tmp.path()`
    // because env-var races between concurrent tokio tests can repoint the
    // global var (see comment above). `tmp` stays in scope so the tempdir
    // we sniffed isn't pulled out from under us mid-request.
    assert!(
        data_dir.join("tenants").join("acme").is_dir(),
        "expected tenant dir under {}",
        data_dir.display()
    );
    let _ = tmp; // keep tempdir alive until end of test
}

#[tokio::test]
async fn create_400_invalid_slug() {
    let (state, _tmp, _guard) = state_enabled().await;
    let app = tenants_routes::router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/admin/tenants")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{
                        "slug": "BAD!!",
                        "admin_username": "alice",
                        "admin_password": "pwd"
                    }"#,
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    let body = body_json(resp).await;
    assert_eq!(body["error"], "invalid_tenant_slug");
    assert!(
        !body["reason"].as_str().unwrap_or("").is_empty(),
        "reason should be populated"
    );
}

#[tokio::test]
async fn create_400_missing_admin_username() {
    let (state, _tmp, _guard) = state_enabled().await;
    let app = tenants_routes::router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/admin/tenants")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{
                        "slug": "acme",
                        "admin_username": "",
                        "admin_password": "pwd"
                    }"#,
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    let body = body_json(resp).await;
    assert_eq!(body["error"], "missing_admin_username");
}

#[tokio::test]
async fn create_400_missing_admin_password() {
    let (state, _tmp, _guard) = state_enabled().await;
    let app = tenants_routes::router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/admin/tenants")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{
                        "slug": "acme",
                        "admin_username": "alice",
                        "admin_password": ""
                    }"#,
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    let body = body_json(resp).await;
    assert_eq!(body["error"], "missing_admin_password");
}

#[tokio::test]
async fn create_409_duplicate() {
    let (state, _tmp, _guard) = state_enabled().await;
    let app = tenants_routes::router(state);

    // First create — happy path.
    let req = || {
        Request::builder()
            .method("POST")
            .uri("/admin/tenants")
            .header("content-type", "application/json")
            .body(Body::from(
                r#"{
                    "slug": "acme",
                    "admin_username": "alice",
                    "admin_password": "pwd"
                }"#,
            ))
            .unwrap()
    };

    let resp1 = app.clone().oneshot(req()).await.unwrap();
    assert_eq!(resp1.status(), StatusCode::CREATED);

    // Second create with the same slug — conflict.
    let resp2 = app.oneshot(req()).await.unwrap();
    assert_eq!(resp2.status(), StatusCode::CONFLICT);
    let body = body_json(resp2).await;
    assert_eq!(body["error"], "tenant_exists");
}

#[tokio::test]
async fn create_403_when_tenants_disabled() {
    // [tenants].enabled = false — POST mirrors the GET disabled path.
    let cfg = Config::default();
    let state = AdminState::new(
        Arc::new(PluginRegistry::default()),
        Arc::new(ArcSwap::from_pointee(cfg)),
    );
    let app = tenants_routes::router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/admin/tenants")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{
                        "slug": "acme",
                        "admin_username": "alice",
                        "admin_password": "pwd"
                    }"#,
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    let body = body_json(resp).await;
    assert_eq!(body["error"], "tenants_disabled");
}

// ---------------------------------------------------------------------------
// Phase 4 W1.5 (next-tasks A4): per-tenant content reads for the diff view
// ---------------------------------------------------------------------------

#[tokio::test]
async fn read_prompt_segment_returns_content_when_file_exists() {
    let (state, tmp, _guard) = state_enabled().await;

    let segment_dir = tmp
        .path()
        .join("tenants")
        .join("acme")
        .join("prompt_segments");
    std::fs::create_dir_all(&segment_dir).unwrap();
    std::fs::write(segment_dir.join("agent.greeting.md"), "Welcome to Acme.").unwrap();

    let app = tenants_routes::router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/admin/tenants/acme/prompt_segments/agent.greeting")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let body = body_json(resp).await;
    assert_eq!(body["tenant_id"], "acme");
    assert_eq!(body["kind"], "prompt_template");
    assert_eq!(body["name"], "agent.greeting");
    assert_eq!(body["exists"], true);
    assert_eq!(body["content"], "Welcome to Acme.");
}

#[tokio::test]
async fn read_prompt_segment_returns_exists_false_for_missing_file() {
    let (state, _tmp, _guard) = state_enabled().await;
    let app = tenants_routes::router(state);

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/admin/tenants/default/prompt_segments/agent.greeting")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let body = body_json(resp).await;
    assert_eq!(body["exists"], false);
    assert_eq!(body["content"], "");
}

#[tokio::test]
async fn read_agent_card_returns_content_when_file_exists() {
    let (state, tmp, _guard) = state_enabled().await;

    let cards_dir = tmp.path().join("tenants").join("acme").join("agent_cards");
    std::fs::create_dir_all(&cards_dir).unwrap();
    std::fs::write(
        cards_dir.join("casual.md"),
        "# Casual\n\nWarm, low-formality.",
    )
    .unwrap();

    let app = tenants_routes::router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/admin/tenants/acme/agent_cards/casual")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let body = body_json(resp).await;
    assert_eq!(body["kind"], "agent_card");
    assert_eq!(body["name"], "casual");
    assert_eq!(body["exists"], true);
    assert!(body["content"].as_str().unwrap().contains("Warm"));
}

#[tokio::test]
async fn read_content_rejects_invalid_tenant_slug_with_400() {
    let (state, _tmp, _guard) = state_enabled().await;
    let app = tenants_routes::router(state);

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/admin/tenants/BAD!!/prompt_segments/agent.greeting")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);

    let body = body_json(resp).await;
    assert_eq!(body["error"], "invalid_tenant_slug");
}

#[tokio::test]
async fn read_content_rejects_invalid_segment_name_with_400() {
    let (state, _tmp, _guard) = state_enabled().await;
    let app = tenants_routes::router(state);

    // The Path extractor URL-decodes `..` and `%2F`. Either the
    // axum router rejects with 404 or the segment validator catches
    // it as 400 — either is acceptable defense-in-depth.
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/admin/tenants/default/prompt_segments/..%2Fetc%2Fpasswd")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert!(
        resp.status() == StatusCode::BAD_REQUEST || resp.status() == StatusCode::NOT_FOUND,
        "expected 4xx for traversal attempt, got {}",
        resp.status()
    );
}

#[tokio::test]
async fn read_content_returns_403_when_tenants_disabled() {
    let _guard = data_dir_lock().lock().await;
    let tmp = TempDir::new().unwrap();
    std::env::set_var("CORLINMAN_DATA_DIR", tmp.path());

    let cfg = Config::default(); // enabled = false by default
    let state = AdminState::new(
        Arc::new(PluginRegistry::default()),
        Arc::new(ArcSwap::from_pointee(cfg)),
    );

    let app = tenants_routes::router(state);
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/admin/tenants/default/prompt_segments/agent.greeting")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    let body = body_json(resp).await;
    assert_eq!(body["error"], "tenants_disabled");
}
