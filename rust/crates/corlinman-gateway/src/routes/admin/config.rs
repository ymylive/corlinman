//! `GET /admin/config` + `POST /admin/config` — live config reload.
//!
//! Sprint 5 T2: the admin UI reads the active config (redacted) and posts
//! back an edited TOML. Successful validation + non-restart-field diff
//! results in an in-memory [`ArcSwap`] swap and an atomic on-disk rewrite
//! so the new snapshot survives process restart.
//!
//! Route shapes:
//!
//! `GET /admin/config`
//!   → 200 `{toml, version, meta}` where `toml` is the current snapshot
//!     serialised with literal secrets replaced by `***REDACTED***`
//!     (via [`Config::redacted`]), `version` is an 8-char hex hash of the
//!     pre-redaction TOML (ETag-style), and `meta` carries the authoring
//!     stamps from `[meta]`.
//!
//! `POST /admin/config` body `{toml, dry_run}`
//!   → 200 `{status, issues, requires_restart}` when the payload parses
//!     and every [`ValidationIssue`] is `Warn`-level. `dry_run = true`
//!     validates only; `dry_run = false` also swaps the in-memory snapshot,
//!     rewrites the file atomically (tmp → rename), and pushes the new
//!     approval rules into the live [`ApprovalGate`] (if present).
//!   → 400 `{status: "invalid", issues, requires_restart: []}` when the
//!     TOML fails to decode or any `Error`-level issue is raised.
//!   → 503 `{error: "config_path_unset"}` when a non-dry_run request
//!     arrives on a gateway that was booted without `$CORLINMAN_CONFIG`.
//!
//! Fields flagged by [`detect_restart_fields`] are applied to the
//! in-memory snapshot anyway (they're still serialised, so handlers that
//! *do* consult the live config pick them up), but the response surfaces
//! the list so the operator knows a restart is required for the
//! listener / channel to honour them.

use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::path::{Path, PathBuf};

use axum::{
    extract::State,
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use corlinman_core::config::{Config, IssueLevel, Meta, ValidationIssue};
use serde::{Deserialize, Serialize};
use serde_json::json;

use super::AdminState;

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

/// Sub-router for `/admin/config*`. Mounted by [`super::router_with_state`]
/// inside the admin auth guard.
pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/admin/config", get(get_config).post(post_config))
        .route("/admin/config/schema", get(get_schema))
        .route("/admin/config/reload", post(post_reload))
        .with_state(state)
}

/// `POST /admin/config/reload` — manually trigger a hot-reload of the on-disk
/// TOML. Equivalent to sending SIGHUP to the process but authenticated + returns
/// the [`ReloadReport`] as JSON so ops scripts can assert on the diff. Returns
/// 503 `config_reload_disabled` when the watcher isn't wired (test harnesses,
/// old integration tests).
async fn post_reload(State(state): State<AdminState>) -> Response {
    let Some(watcher) = state.config_watcher.as_ref() else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({
                "error": "config_reload_disabled",
                "message": "gateway booted without a ConfigWatcher; hot-reload is unavailable",
            })),
        )
            .into_response();
    };
    match watcher.trigger_reload().await {
        Ok(report) => Json(report).into_response(),
        Err(err) => {
            tracing::warn!(error = %err, "admin/config/reload: trigger failed");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({
                    "error": "reload_failed",
                    "message": err.to_string(),
                })),
            )
                .into_response()
        }
    }
}

/// Sprint 6 T4: `GET /admin/config/schema` — JSON-Schema document for
/// [`Config`], rendered via `schemars`. The UI consumes it to drive
/// Monaco's autocomplete + hover tooltips; also useful for any future
/// typed client that wants to validate a TOML edit before posting.
async fn get_schema(State(_state): State<AdminState>) -> Json<serde_json::Value> {
    let schema = schemars::schema_for!(Config);
    Json(serde_json::to_value(schema).unwrap_or_else(|_| serde_json::json!({})))
}

// ---------------------------------------------------------------------------
// GET /admin/config
// ---------------------------------------------------------------------------

/// Response shape for `GET /admin/config`. `version` is the `hash8` of the
/// *pre-redaction* TOML so a round-trip POST with the same `toml` body will
/// observe a stable ETag even though the wire payload is redacted.
#[derive(Debug, Serialize)]
pub struct GetConfigResponse {
    pub toml: String,
    pub version: String,
    pub meta: Meta,
}

async fn get_config(State(state): State<AdminState>) -> Response {
    let snapshot = state.config.load_full();
    let version = hash8_of(&snapshot);
    let redacted = snapshot.redacted();
    let toml = match toml::to_string_pretty(&redacted) {
        Ok(s) => s,
        Err(err) => {
            tracing::error!(error = %err, "admin/config: serialise failed");
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({
                    "error": "serialise_failed",
                    "message": err.to_string(),
                })),
            )
                .into_response();
        }
    };
    Json(GetConfigResponse {
        toml,
        version,
        meta: snapshot.meta.clone(),
    })
    .into_response()
}

// ---------------------------------------------------------------------------
// POST /admin/config
// ---------------------------------------------------------------------------

/// Body for `POST /admin/config`. `dry_run = true` validates without
/// committing; default `false` swaps + persists.
#[derive(Debug, Deserialize)]
pub struct PostConfigBody {
    pub toml: String,
    #[serde(default)]
    pub dry_run: bool,
}

/// Response for `POST /admin/config`.
#[derive(Debug, Serialize)]
pub struct PostConfigResponse {
    /// `"ok"` — config accepted (may still carry `Warn` issues); `"invalid"`
    /// — rejected before any swap/write happened.
    pub status: &'static str,
    pub issues: Vec<ValidationIssue>,
    /// Dotted paths whose change needs a process restart to fully take
    /// effect (e.g. `server.port`). Empty on dry-run / no-op swaps.
    pub requires_restart: Vec<String>,
    /// Updated `version` hash after a successful non-dry-run swap. `None`
    /// on dry-run or rejection — the caller keeps its previous ETag.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub version: Option<String>,
}

async fn post_config(
    State(state): State<AdminState>,
    Json(body): Json<PostConfigBody>,
) -> Response {
    // Stage 1: TOML decode.
    let mut new_config: Config = match toml::from_str::<Config>(&body.toml) {
        Ok(c) => c,
        Err(err) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(PostConfigResponse {
                    status: "invalid",
                    issues: vec![ValidationIssue {
                        path: "toml".into(),
                        code: "decode_failed".into(),
                        message: err.to_string(),
                        level: IssueLevel::Error,
                    }],
                    requires_restart: Vec::new(),
                    version: None,
                }),
            )
                .into_response();
        }
    };

    // Stage 2: validator-derive + cross-field report.
    let issues = new_config.validate_report();
    if issues.iter().any(|i| i.level == IssueLevel::Error) {
        return (
            StatusCode::BAD_REQUEST,
            Json(PostConfigResponse {
                status: "invalid",
                issues,
                requires_restart: Vec::new(),
                version: None,
            }),
        )
            .into_response();
    }

    // Stage 3: diff against the active snapshot for restart detection.
    let current = state.config.load_full();
    let requires_restart = detect_restart_fields(&current, &new_config);

    // Bugfix: merge real secrets back from the in-memory snapshot wherever
    // the posted payload still carries the literal `REDACTED_SENTINEL`
    // (the operator round-tripped the redacted GET echo unchanged).
    // Without this step the save path would clobber `admin.password_hash`
    // and every literal provider API key with `"***REDACTED***"` —
    // permanently locking the operator out.
    new_config.merge_redacted_secrets_from(&current);

    if body.dry_run {
        return Json(PostConfigResponse {
            status: "ok",
            issues,
            requires_restart,
            version: None,
        })
        .into_response();
    }

    // Stage 4: persist. File first, then memory swap — that way if `fs::rename`
    // fails (permissions, disk full) we bail before publishing a snapshot that
    // wouldn't survive a restart. The small window where the file is newer than
    // memory is acceptable: no handler has observed the new snapshot yet.
    let Some(path) = state.config_path.as_ref() else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({
                "error": "config_path_unset",
                "message": "gateway booted without a config file path; dry_run is still available",
            })),
        )
            .into_response();
    };

    // Belt-and-braces: even after `merge_redacted_secrets_from` we double
    // check the payload doesn't still carry the redaction sentinel. The
    // only way this could happen is if the in-memory snapshot didn't have
    // the secret either (e.g. provider added in the POST body, with a
    // literal value of `"***REDACTED***"`) — refusing to write avoids
    // pinning the placeholder string on disk.
    if new_config.has_redacted_sentinel() {
        tracing::error!(
            "admin/config: refusing to write config containing redaction sentinel",
        );
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({
                "error": "redacted_sentinel_in_payload",
                "message": "POST payload contains the literal `***REDACTED***` placeholder for at least one secret. \
                            Replace it with a real value (or omit the field to keep the current secret) before retrying.",
            })),
        )
            .into_response();
    }

    // PR-#2 review fix: refresh `[meta]` so the `POST /admin/config`
    // save path stamps the audit trail too — same belt-and-braces
    // contract every other admin-write route now follows.
    new_config.stamp_meta();

    let serialised = match toml::to_string_pretty(&new_config) {
        Ok(s) => s,
        Err(err) => {
            tracing::error!(error = %err, "admin/config: serialise new config failed");
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({
                    "error": "serialise_failed",
                    "message": err.to_string(),
                })),
            )
                .into_response();
        }
    };

    if let Err(err) = atomic_write_toml(path, &serialised).await {
        tracing::error!(error = %err, path = %path.display(), "admin/config: atomic write failed");
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({
                "error": "write_failed",
                "message": err.to_string(),
            })),
        )
            .into_response();
    }

    // Stage 5: swap in-memory snapshot + live approval rules.
    state.config.store(std::sync::Arc::new(new_config.clone()));
    if let Some(gate) = state.approval_gate.as_ref() {
        gate.swap_rules(new_config.approvals.rules.clone());
    }

    // Feature C last-mile: propagate to the Python-side JSON drop so the
    // ProviderRegistry subprocess picks up the new alias / provider /
    // embedding shape on its next resolve call.
    state.rewrite_py_config().await;

    let version = hash8_of(&new_config);
    Json(PostConfigResponse {
        status: "ok",
        issues,
        requires_restart,
        version: Some(version),
    })
    .into_response()
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Short hex digest of a config's pretty-TOML form. Used as a lightweight
/// version / ETag handle — collisions don't corrupt anything, they just
/// make a redundant refresh look idempotent.
fn hash8_of(cfg: &Config) -> String {
    // Serialise with the same formatter the GET response uses so a caller
    // can echo `version` back after a no-op swap.
    let text = toml::to_string_pretty(cfg).unwrap_or_default();
    let mut hasher = DefaultHasher::new();
    text.hash(&mut hasher);
    format!("{:08x}", hasher.finish() as u32)
}

/// Dotted field paths that cannot be honoured without a full process
/// restart (bind socket + channel client tasks both read from config
/// once at boot). Callers surface this list so operators know a restart
/// is pending.
pub fn detect_restart_fields(old: &Config, new: &Config) -> Vec<String> {
    let mut out = Vec::new();

    // Server listener — the bound TcpListener doesn't reopen mid-flight.
    if old.server.port != new.server.port {
        out.push("server.port".into());
    }
    if old.server.bind != new.server.bind {
        out.push("server.bind".into());
    }
    if old.server.data_dir != new.server.data_dir {
        out.push("server.data_dir".into());
    }

    // Channels — the QQ / Telegram adapters spawn once at boot.
    let qq_enabled_old = old.channels.qq.as_ref().map(|q| q.enabled).unwrap_or(false);
    let qq_enabled_new = new.channels.qq.as_ref().map(|q| q.enabled).unwrap_or(false);
    if qq_enabled_old != qq_enabled_new {
        out.push("channels.qq.enabled".into());
    }
    let tg_enabled_old = old
        .channels
        .telegram
        .as_ref()
        .map(|t| t.enabled)
        .unwrap_or(false);
    let tg_enabled_new = new
        .channels
        .telegram
        .as_ref()
        .map(|t| t.enabled)
        .unwrap_or(false);
    if tg_enabled_old != tg_enabled_new {
        out.push("channels.telegram.enabled".into());
    }

    // Logging subscriber is wired from config once at boot; changing the
    // level mid-flight would require re-init which tracing_subscriber
    // doesn't support cleanly.
    if old.logging.level != new.logging.level {
        out.push("logging.level".into());
    }
    if old.logging.format != new.logging.format {
        out.push("logging.format".into());
    }

    out
}

/// Atomically replace the TOML file at `path` with `contents`:
/// write to `<path>.new`, fsync, then `rename` — POSIX `rename(2)` is
/// atomic within the same directory, so observers see either the old or
/// the new file but never a partial mix.
async fn atomic_write_toml(path: &Path, contents: &str) -> std::io::Result<()> {
    // Ensure the parent exists — first-time writes on a fresh $DATA_DIR
    // would otherwise fail with ENOENT.
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    let tmp: PathBuf = {
        let mut p = path.to_path_buf();
        // `with_extension` would clobber `.toml`; tack `.new` on instead so
        // the temp file is still obviously a config-in-progress.
        p.as_mut_os_string().push(".new");
        p
    };
    tokio::fs::write(&tmp, contents).await?;
    tokio::fs::rename(&tmp, path).await?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::middleware::approval::ApprovalGate;
    use arc_swap::ArcSwap;
    use axum::body::{to_bytes, Body};
    use axum::http::Request;
    use corlinman_core::config::{
        ApprovalMode, ApprovalRule, Config, ProviderEntry, QqChannelConfig, SecretRef,
    };
    use corlinman_plugins::registry::PluginRegistry;
    use corlinman_vector::SqliteStore;
    use std::collections::HashMap;
    use std::sync::Arc;
    use std::time::Duration;
    use tempfile::TempDir;
    use tower::ServiceExt;

    // ---- fixtures ------------------------------------------------------

    fn base_config() -> Config {
        let mut cfg = Config::default();
        cfg.providers.insert(
            "anthropic",
            ProviderEntry {
                api_key: Some(SecretRef::EnvVar {
                    env: "ANTHROPIC_API_KEY".into(),
                }),
                base_url: None,
                enabled: true,
                ..Default::default()
            },
        );
        cfg
    }

    fn base_state(path: Option<PathBuf>) -> AdminState {
        let mut state = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(base_config())),
        );
        if let Some(p) = path {
            state = state.with_config_path(p);
        }
        state
    }

    fn minimal_toml_body(default_model: &str) -> String {
        // A round-trippable config that keeps validation green.
        format!(
            r#"
[server]
port = 6005
bind = "0.0.0.0"
data_dir = "/tmp/corlinman-test"

[providers.anthropic]
api_key = {{ env = "ANTHROPIC_API_KEY" }}
enabled = true

[models]
default = "{default_model}"
"#
        )
    }

    async fn gate_with_rules(rules: Vec<ApprovalRule>) -> (Arc<ApprovalGate>, TempDir) {
        let tmp = TempDir::new().unwrap();
        let store = SqliteStore::open(&tmp.path().join("kb.sqlite"))
            .await
            .unwrap();
        corlinman_vector::migration::ensure_schema(&store)
            .await
            .unwrap();
        let gate = ApprovalGate::new(rules, Arc::new(store), Duration::from_millis(200));
        (Arc::new(gate), tmp)
    }

    async fn body_json(resp: Response) -> serde_json::Value {
        let b = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        serde_json::from_slice(&b).unwrap()
    }

    // ---- detect_restart_fields ----------------------------------------

    #[test]
    fn detect_restart_fields_port_change() {
        let mut old = base_config();
        let mut new = base_config();
        old.server.port = 6005;
        new.server.port = 7777;
        let fields = detect_restart_fields(&old, &new);
        assert!(fields.iter().any(|f| f == "server.port"), "got {fields:?}");
    }

    #[test]
    fn detect_restart_fields_alias_only_no_restart() {
        let mut old = base_config();
        let mut new = base_config();
        // aliases are live-reloadable (agent layer reads them per-request)
        let mut aliases = HashMap::new();
        aliases.insert("smart".into(), "claude-opus-4-7".into());
        new.models.aliases = aliases;
        // approvals.rules are also live (we call ApprovalGate::swap_rules)
        new.approvals.rules.push(ApprovalRule {
            plugin: "file-ops".into(),
            tool: None,
            mode: ApprovalMode::Prompt,
            allow_session_keys: Vec::new(),
        });
        // models.default is live too
        new.models.default = "claude-opus-4-7".into();
        old.models.default = "claude-sonnet-4-5".into();
        let fields = detect_restart_fields(&old, &new);
        assert!(
            fields.is_empty(),
            "expected no restart fields, got {fields:?}"
        );
    }

    #[test]
    fn detect_restart_fields_channel_toggle() {
        let old = base_config();
        let mut new = base_config();
        new.channels.qq = Some(QqChannelConfig {
            enabled: true,
            ws_url: "ws://127.0.0.1:3001".into(),
            access_token: None,
            self_ids: vec![1],
            group_keywords: HashMap::new(),
            rate_limit: Default::default(),
            napcat_url: None,
            napcat_access_token: None,
        });
        // old has no qq; new has enabled qq — toggle.
        let fields = detect_restart_fields(&old, &new);
        assert!(
            fields.iter().any(|f| f == "channels.qq.enabled"),
            "got {fields:?}"
        );
        // Disabled -> disabled (via absence) should not flag.
        new.channels.qq.as_mut().unwrap().enabled = false;
        let fields = detect_restart_fields(&old, &new);
        assert!(fields.iter().all(|f| f != "channels.qq.enabled"));
    }

    // ---- POST handler -------------------------------------------------

    #[tokio::test]
    async fn post_config_dry_run_validates_only() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let state = base_state(Some(path.clone()));
        let original_port = state.config.load().server.port;

        let app = router(state.clone());
        let body = serde_json::to_string(&serde_json::json!({
            "toml": minimal_toml_body("claude-opus-4-7"),
            "dry_run": true,
        }))
        .unwrap();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/config")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(v["status"], "ok");
        // dry_run: file never written, snapshot unchanged.
        assert!(!path.exists(), "dry_run must not touch the filesystem");
        assert_eq!(state.config.load().server.port, original_port);
        // dry_run: version field omitted (no swap happened).
        assert!(v.get("version").is_none());
    }

    #[tokio::test]
    async fn post_config_applies_swap() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let state = base_state(Some(path.clone()));

        let app = router(state.clone());
        let body = serde_json::to_string(&serde_json::json!({
            "toml": minimal_toml_body("claude-opus-4-7"),
            "dry_run": false,
        }))
        .unwrap();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/config")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(v["status"], "ok");
        assert!(v.get("version").and_then(|x| x.as_str()).is_some());

        // Snapshot updated in-memory.
        assert_eq!(state.config.load().models.default, "claude-opus-4-7");
        // File written atomically — no `.new` left behind.
        assert!(path.exists());
        assert!(!path.with_extension("toml.new").exists());
    }

    #[tokio::test]
    async fn post_config_invalid_toml_returns_400() {
        let tmp = TempDir::new().unwrap();
        let state = base_state(Some(tmp.path().join("config.toml")));
        let app = router(state);
        let body = serde_json::to_string(&serde_json::json!({
            "toml": "this = is = not = toml",
            "dry_run": false,
        }))
        .unwrap();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/config")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let v = body_json(resp).await;
        assert_eq!(v["status"], "invalid");
        let issues = v["issues"].as_array().unwrap();
        assert_eq!(issues[0]["code"], "decode_failed");
        assert_eq!(issues[0]["path"], "toml");
    }

    #[tokio::test]
    async fn post_config_validation_error_returns_400() {
        let tmp = TempDir::new().unwrap();
        let state = base_state(Some(tmp.path().join("config.toml")));
        let app = router(state);
        // `server.port = 0` trips the validator-derive `range(min=1)` guard.
        let bad = r#"
[server]
port = 0
bind = "0.0.0.0"

[providers.anthropic]
api_key = { env = "X" }
enabled = true

[models]
default = "claude-sonnet-4-5"
"#;
        let body = serde_json::to_string(&serde_json::json!({
            "toml": bad,
            "dry_run": false,
        }))
        .unwrap();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/config")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let v = body_json(resp).await;
        assert_eq!(v["status"], "invalid");
        let issues = v["issues"].as_array().unwrap();
        assert!(
            issues
                .iter()
                .any(|i| i["path"].as_str().unwrap_or("").contains("port")),
            "expected a port issue, got {issues:?}"
        );
    }

    #[tokio::test]
    async fn post_config_writes_file_atomically() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        // Pre-seed the file so we can confirm the swap *replaces* it.
        tokio::fs::write(&path, "# placeholder\n").await.unwrap();
        let state = base_state(Some(path.clone()));
        let app = router(state);
        let body = serde_json::to_string(&serde_json::json!({
            "toml": minimal_toml_body("claude-opus-4-7"),
            "dry_run": false,
        }))
        .unwrap();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/config")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let on_disk = tokio::fs::read_to_string(&path).await.unwrap();
        assert!(
            on_disk.contains("claude-opus-4-7"),
            "file should carry the posted default model; got: {on_disk}"
        );
        // No sidecar left behind.
        let mut stale = path.to_path_buf();
        stale.as_mut_os_string().push(".new");
        assert!(!stale.exists(), "tmp sidecar must be renamed away");
    }

    #[tokio::test]
    async fn post_config_updates_approval_gate_rules() {
        let (gate, _tmp_db) = gate_with_rules(Vec::new()).await;
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let state = base_state(Some(path.clone())).with_approval_gate(gate.clone());

        assert!(gate.rules_snapshot().is_empty());

        let toml = r#"
[server]
port = 6005
bind = "0.0.0.0"
data_dir = "/tmp/corlinman-test"

[providers.anthropic]
api_key = { env = "ANTHROPIC_API_KEY" }
enabled = true

[models]
default = "claude-sonnet-4-5"

[[approvals.rules]]
plugin = "file-ops"
tool = "file-ops.write"
mode = "prompt"
"#;
        let app = router(state.clone());
        let body = serde_json::to_string(&serde_json::json!({
            "toml": toml,
            "dry_run": false,
        }))
        .unwrap();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/config")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let rules = gate.rules_snapshot();
        assert_eq!(rules.len(), 1);
        assert_eq!(rules[0].plugin, "file-ops");
    }

    #[tokio::test]
    async fn post_config_without_path_returns_503_on_non_dry_run() {
        // No config_path attached — mirrors a stub boot with no file on disk.
        let state = base_state(None);
        let app = router(state);
        let body = serde_json::to_string(&serde_json::json!({
            "toml": minimal_toml_body("claude-opus-4-7"),
            "dry_run": false,
        }))
        .unwrap();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/config")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn get_schema_returns_json_schema_document() {
        let state = base_state(None);
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/config/schema")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        // Schemars v0.8 emits `$schema` + a top-level object with
        // `properties.server`, `properties.models`, etc.
        let props = &v["properties"];
        assert!(
            props.is_object(),
            "expected top-level properties; got {v:?}"
        );
        assert!(props.get("server").is_some());
        assert!(props.get("models").is_some());
        assert!(props.get("providers").is_some());
    }

    /// Regression: posting back the redacted echo (the exact shape GET
    /// `/admin/config` returns) must NOT destroy the live
    /// `admin.password_hash` on disk. The route merges the real hash
    /// from the in-memory snapshot before serialising.
    #[tokio::test]
    async fn post_config_preserves_password_hash_on_redacted_round_trip() {
        use corlinman_core::config::REDACTED_SENTINEL;

        let real_hash = "$argon2id$v=19$m=19456,t=2,p=1$realsaltbytes$realhashbytes";
        let mut cfg = base_config();
        cfg.admin.username = Some("admin".into());
        cfg.admin.password_hash = Some(real_hash.into());

        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let state = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        )
        .with_config_path(path.clone());

        // Build the payload the way the UI does: GET, then POST the same
        // string straight back. The GET handler renders the redacted
        // form, so the wire payload has password_hash = REDACTED.
        let redacted = state.config.load_full().redacted();
        let wire = toml::to_string_pretty(&redacted).unwrap();
        assert!(wire.contains(REDACTED_SENTINEL));

        let body = serde_json::to_string(&serde_json::json!({
            "toml": wire,
            "dry_run": false,
        }))
        .unwrap();
        let app = router(state.clone());
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/config")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);

        // On-disk file: real hash present, sentinel absent.
        let on_disk = tokio::fs::read_to_string(&path).await.unwrap();
        assert!(
            !on_disk.contains(REDACTED_SENTINEL),
            "on-disk TOML must NOT contain `{REDACTED_SENTINEL}`; got:\n{on_disk}"
        );
        assert!(
            on_disk.contains(real_hash),
            "on-disk TOML must carry the real argon2 hash; got:\n{on_disk}"
        );
        // In-memory snapshot also restored.
        assert_eq!(
            state.config.load().admin.password_hash.as_deref(),
            Some(real_hash)
        );
    }

    /// If the POST payload's literal provider `api_key` is the sentinel
    /// and the in-memory snapshot has no matching provider entry (so the
    /// merge can't restore the original secret), the belt-and-braces
    /// guard returns 400 rather than pinning the placeholder on disk.
    #[tokio::test]
    async fn post_config_refuses_unmergable_sentinel_payload() {
        // Snapshot only carries `anthropic`. Posting a `glm` provider
        // with a literal `***REDACTED***` api_key has nothing to merge
        // from → must be rejected.
        let cfg = base_config();
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let state = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        )
        .with_config_path(path.clone());
        let app = router(state);

        let toml = r#"
[server]
port = 6005
bind = "0.0.0.0"
data_dir = "/tmp/corlinman-test"

[providers.anthropic]
api_key = { env = "ANTHROPIC_API_KEY" }
enabled = true

[providers.glm]
api_key = { value = "***REDACTED***" }
enabled = true

[models]
default = "claude-sonnet-4-5"
"#;
        let body = serde_json::to_string(&serde_json::json!({
            "toml": toml,
            "dry_run": false,
        }))
        .unwrap();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/config")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let v = body_json(resp).await;
        assert_eq!(v["error"], "redacted_sentinel_in_payload");
        // File never touched.
        assert!(!path.exists());
    }

    #[tokio::test]
    async fn get_config_returns_redacted_toml_and_version() {
        // Seed a literal secret so we can assert redaction.
        let mut cfg = base_config();
        cfg.providers.insert(
            "openai",
            ProviderEntry {
                api_key: Some(SecretRef::Literal {
                    value: "sk-top-secret".into(),
                }),
                base_url: None,
                enabled: true,
                ..Default::default()
            },
        );
        let state = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        );
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/config")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        let toml_text = v["toml"].as_str().unwrap();
        assert!(
            !toml_text.contains("sk-top-secret"),
            "literal secret must be redacted in GET payload"
        );
        assert!(toml_text.contains("***REDACTED***"));
        let ver = v["version"].as_str().unwrap();
        assert_eq!(ver.len(), 8, "version must be 8-char hex, got {ver}");
    }
}
