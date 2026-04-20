//! Sprint 5 T2 — live `POST /admin/config` integration test.
//!
//! Drives the admin router end-to-end against a real [`ArcSwap<Config>`]
//! and an on-disk TOML file, asserting that:
//!
//!   1. `GET /admin/config` echoes the current snapshot (redacted) with a
//!      stable `version` / ETag.
//!   2. A `dry_run = true` POST validates without touching the file or the
//!      in-memory snapshot, and omits `version` in the response.
//!   3. A `dry_run = false` POST that adds a new model alias swaps the
//!      snapshot immediately (next `GET` shows it), rewrites the file
//!      atomically, and bumps the `version` hash.
//!   4. A TOML with `server.port` changed surfaces `"server.port"` in
//!      `requires_restart` but still gets applied.
//!
//! The test skips auth (basic-auth is covered by unit tests); it builds
//! the guarded router directly through `admin::router_with_state` without
//! attaching admin credentials, then issues requests against the pre-auth
//! surface via the `auth::router` merge. To exercise `/admin/config` past
//! the guard we wire valid Basic credentials into the state and pass them
//! on every request — same pattern as `approval_gate_e2e.rs`.

use std::sync::Arc;

use arc_swap::ArcSwap;
use axum::body::{to_bytes, Body};
use axum::http::{header, Request, StatusCode};
use base64::Engine;
use corlinman_core::config::Config;
use corlinman_gateway::routes::admin::{router_with_state, AdminState};
use corlinman_plugins::registry::PluginRegistry;
use serde_json::{json, Value};
use tower::ServiceExt;

const ADMIN_USER: &str = "admin";
const ADMIN_PASS: &str = "secret";

fn hash_password(password: &str) -> String {
    use argon2::password_hash::{PasswordHasher, SaltString};
    let salt = SaltString::encode_b64(b"corlinman_test_salt_bytes_16").unwrap();
    argon2::Argon2::default()
        .hash_password(password.as_bytes(), &salt)
        .unwrap()
        .to_string()
}

fn admin_basic_header() -> String {
    format!(
        "Basic {}",
        base64::engine::general_purpose::STANDARD.encode(format!("{ADMIN_USER}:{ADMIN_PASS}"))
    )
}

/// Stable data_dir the POST bodies reference — keep it aligned with the
/// seed config so `detect_restart_fields` doesn't trip on `server.data_dir`.
const TEST_DATA_DIR: &str = "/tmp/corlinman-live-reload-it";

fn seed_config() -> Config {
    let mut cfg = Config::default();
    cfg.server.data_dir = std::path::PathBuf::from(TEST_DATA_DIR);
    cfg.admin.username = Some(ADMIN_USER.into());
    cfg.admin.password_hash = Some(hash_password(ADMIN_PASS));
    // Provider so validate_report doesn't raise any errors on the round-trip.
    cfg.providers.anthropic = Some(corlinman_core::config::ProviderEntry {
        api_key: Some(corlinman_core::config::SecretRef::EnvVar {
            env: "ANTHROPIC_API_KEY".into(),
        }),
        base_url: None,
        enabled: true,
    });
    cfg
}

/// Full TOML body that validates cleanly. Takes a map of override fragments
/// so callers can mutate the single field they care about.
fn base_toml(port: u16, default_model: &str, extra_alias: Option<(&str, &str)>) -> String {
    let mut aliases = String::new();
    if let Some((k, v)) = extra_alias {
        aliases.push_str(&format!("\n[models.aliases]\n{k} = \"{v}\"\n"));
    }
    format!(
        r#"
[server]
port = {port}
bind = "0.0.0.0"
data_dir = "{TEST_DATA_DIR}"

[admin]
username = "{ADMIN_USER}"
password_hash = "{hash}"

[providers.anthropic]
api_key = {{ env = "ANTHROPIC_API_KEY" }}
enabled = true

[models]
default = "{default_model}"
{aliases}
"#,
        hash = hash_password(ADMIN_PASS),
    )
}

async fn body_json(resp: axum::response::Response) -> Value {
    let b = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    serde_json::from_slice(&b).unwrap()
}

#[tokio::test]
async fn live_reload_end_to_end() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("config.toml");
    // Seed the file so the on-disk view matches the in-memory one from
    // the start — the first POST will replace it.
    tokio::fs::write(&path, "# placeholder\n").await.unwrap();

    let cfg = Arc::new(ArcSwap::from_pointee(seed_config()));
    let state = AdminState::new(Arc::new(PluginRegistry::default()), cfg.clone())
        .with_config_path(path.clone());
    let app = router_with_state(state);

    // 1) Initial GET → snapshot, version, redacted.
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/admin/config")
                .header(header::AUTHORIZATION, admin_basic_header())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let initial = body_json(resp).await;
    let initial_version = initial["version"].as_str().unwrap().to_string();
    assert_eq!(initial_version.len(), 8);
    // password_hash is redacted in the GET payload even though the POST
    // body carries the real hash below.
    let initial_toml = initial["toml"].as_str().unwrap();
    assert!(
        initial_toml.contains("***REDACTED***"),
        "admin.password_hash should be redacted; got: {initial_toml}"
    );

    // 2) Dry-run POST: issues must surface but nothing persists.
    let posted_toml = base_toml(6005, "claude-opus-4-7", Some(("smart", "claude-opus-4-7")));
    let dry_body = serde_json::to_string(&json!({
        "toml": posted_toml,
        "dry_run": true,
    }))
    .unwrap();
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/admin/config")
                .header("content-type", "application/json")
                .header(header::AUTHORIZATION, admin_basic_header())
                .body(Body::from(dry_body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let v = body_json(resp).await;
    assert_eq!(v["status"], "ok");
    // dry_run response carries no version field.
    assert!(v.get("version").map(|x| x.is_null()).unwrap_or(true));
    // Snapshot still unchanged.
    let snap = cfg.load();
    assert_eq!(snap.models.default, "claude-sonnet-4-5");
    assert!(snap.models.aliases.is_empty());
    // File unchanged (still the placeholder we seeded).
    let on_disk = tokio::fs::read_to_string(&path).await.unwrap();
    assert_eq!(on_disk, "# placeholder\n");

    // 3) Real POST: applies alias + default_model. No restart expected.
    let apply_body = serde_json::to_string(&json!({
        "toml": posted_toml,
        "dry_run": false,
    }))
    .unwrap();
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/admin/config")
                .header("content-type", "application/json")
                .header(header::AUTHORIZATION, admin_basic_header())
                .body(Body::from(apply_body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let v = body_json(resp).await;
    assert_eq!(v["status"], "ok");
    let new_version = v["version"].as_str().unwrap().to_string();
    assert_ne!(new_version, initial_version, "swap must bump the hash");
    assert!(v["requires_restart"].as_array().unwrap().is_empty());

    // Subsequent GET reflects the swap.
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/admin/config")
                .header(header::AUTHORIZATION, admin_basic_header())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let after = body_json(resp).await;
    assert_eq!(after["version"], new_version);
    let after_toml = after["toml"].as_str().unwrap();
    assert!(after_toml.contains("claude-opus-4-7"));
    assert!(after_toml.contains("smart"));

    // Snapshot: the live ArcSwap observers see the new alias.
    let snap = cfg.load();
    assert_eq!(snap.models.default, "claude-opus-4-7");
    assert_eq!(
        snap.models.aliases.get("smart").map(|s| s.as_str()),
        Some("claude-opus-4-7")
    );

    // File: atomic rewrite landed, sidecar gone.
    let on_disk_after = tokio::fs::read_to_string(&path).await.unwrap();
    assert!(on_disk_after.contains("claude-opus-4-7"));
    let mut sidecar = path.clone();
    sidecar.as_mut_os_string().push(".new");
    assert!(!sidecar.exists(), "tmp sidecar must be renamed away");

    // 4) Port change surfaces in requires_restart.
    let port_body = serde_json::to_string(&json!({
        "toml": base_toml(7777, "claude-opus-4-7", Some(("smart", "claude-opus-4-7"))),
        "dry_run": false,
    }))
    .unwrap();
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/admin/config")
                .header("content-type", "application/json")
                .header(header::AUTHORIZATION, admin_basic_header())
                .body(Body::from(port_body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let v = body_json(resp).await;
    assert_eq!(v["status"], "ok");
    let restart: Vec<String> = v["requires_restart"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_str().unwrap().to_string())
        .collect();
    assert!(
        restart.iter().any(|f| f == "server.port"),
        "expected server.port in requires_restart, got {restart:?}"
    );
    // Snapshot still updates (the TcpListener just won't re-bind).
    assert_eq!(cfg.load().server.port, 7777);
}
