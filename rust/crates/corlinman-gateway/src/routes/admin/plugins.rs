//! `GET /admin/plugins` + `GET /admin/plugins/:name`.
//!
//! Read-only views onto the plugin registry. The UI consumes these on the
//! Plugins page (list table → row detail drawer).
//!
//! Phase 4 W3 C2 iter 8: this module additionally exports
//! [`mcp_admin_router`] — a sub-router that exposes
//! `POST /admin/plugins/:name/disable|enable|restart` against an
//! [`Arc<McpAdapter>`]. The router is intentionally state-disjoint
//! from [`AdminState`]: the gateway wiring step (Wave 4 follow-up)
//! will merge it into the admin tree once the boot path constructs an
//! `McpAdapter` and stores it in app state. Until then the router is
//! exercised by the unit tests in this file (which spawn a real awk
//! MCP fixture) and by integration callers that explicitly opt in.

use std::sync::Arc;

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use corlinman_plugins::manifest::PluginType;
use corlinman_plugins::registry::{Diagnostic, PluginEntry};
use corlinman_plugins::runtime::jsonrpc_stdio::execute as jsonrpc_execute;
use corlinman_plugins::runtime::mcp::adapter::{AdapterError, McpAdapter};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio_util::sync::CancellationToken;

use super::AdminState;

/// One row in the admin plugins table.
///
/// Field names are deliberately chosen to match the existing
/// `ui/lib/api.ts::PluginSummary` surface so the UI does not need a
/// migration on M6 cutover.
#[derive(Debug, Serialize)]
pub struct PluginSummaryOut {
    pub name: String,
    pub version: String,
    pub status: &'static str,
    pub plugin_type: &'static str,
    pub origin: &'static str,
    pub tool_count: usize,
    pub manifest_path: String,
    pub description: String,
    pub capabilities: Vec<String>,
    pub shadowed_count: usize,
}

impl From<&PluginEntry> for PluginSummaryOut {
    fn from(entry: &PluginEntry) -> Self {
        let m = &entry.manifest;
        Self {
            name: m.name.clone(),
            version: m.version.clone(),
            // Status is always "loaded" for M6 — the registry only stores
            // successfully-parsed manifests. Disabled / error states arrive
            // once we track per-plugin health + config-driven disables.
            status: "loaded",
            plugin_type: plugin_type_str(m.plugin_type),
            origin: entry.origin.as_str(),
            tool_count: m.capabilities.tools.len(),
            manifest_path: entry.manifest_path.to_string_lossy().into_owned(),
            description: m.description.clone(),
            capabilities: m
                .capabilities
                .tools
                .iter()
                .map(|t| t.name.clone())
                .collect(),
            shadowed_count: entry.shadowed_count,
        }
    }
}

fn plugin_type_str(t: PluginType) -> &'static str {
    // Delegate to the canonical `PluginType::as_str` so a fourth runtime
    // (e.g. `Mcp` introduced in manifest v3) doesn't require a gateway
    // edit; matching here was a duplication of the same string table.
    t.as_str()
}

/// Sub-router for `/admin/plugins*`. Consumes [`AdminState`] via axum's
/// typed `State` extractor.
pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/admin/plugins", get(list_plugins))
        .route("/admin/plugins/:name", get(get_plugin))
        .route("/admin/plugins/:name/invoke", post(invoke_plugin))
        .with_state(state)
}

async fn list_plugins(State(state): State<AdminState>) -> Json<Vec<PluginSummaryOut>> {
    let rows: Vec<PluginSummaryOut> = state.plugins.list().iter().map(Into::into).collect();
    Json(rows)
}

/// Response for `GET /admin/plugins/:name`.
#[derive(Debug, Serialize)]
struct PluginDetail {
    summary: PluginSummaryOut,
    /// Full TOML-decoded manifest (serialised back out as JSON so the UI can
    /// render arbitrary schemas without a typed client).
    manifest: Value,
    diagnostics: Vec<Value>,
}

async fn get_plugin(
    State(state): State<AdminState>,
    Path(name): Path<String>,
) -> axum::response::Response {
    let Some(entry) = state.plugins.get(&name) else {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({
                "error": "not_found",
                "resource": "plugin",
                "id": name,
            })),
        )
            .into_response();
    };

    let manifest_json = match serde_json::to_value(&*entry.manifest) {
        Ok(v) => v,
        Err(err) => {
            tracing::error!(error = %err, plugin = %name, "manifest -> json failed");
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({
                    "error": "manifest_serialise_failed",
                    "detail": err.to_string(),
                })),
            )
                .into_response();
        }
    };

    // Only surface diagnostics that mention this plugin; keeps the payload
    // small and avoids leaking unrelated collisions.
    let diagnostics: Vec<Value> = state
        .plugins
        .diagnostics()
        .iter()
        .filter_map(|d| diagnostic_for(&name, d))
        .collect();

    Json(PluginDetail {
        summary: (&entry).into(),
        manifest: manifest_json,
        diagnostics,
    })
    .into_response()
}

// ---------------------------------------------------------------------------
// POST /admin/plugins/:name/invoke — Sprint 6 T6 test-invoke endpoint.
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct InvokeBody {
    /// Tool name as declared in `capabilities.tools[*].name`. Required.
    pub tool: String,
    /// Tool arguments as raw JSON. Defaults to `{}`.
    #[serde(default = "default_args")]
    pub arguments: serde_json::Value,
    /// Optional session-key override for the call; defaults to
    /// `"admin-invoke"` which is distinct from any channel-bound session.
    #[serde(default)]
    pub session_key: Option<String>,
    /// Optional deadline override in milliseconds. Handler clamps to 60_000
    /// so a hung plugin can't tie up the admin request indefinitely.
    #[serde(default)]
    pub timeout_ms: Option<u64>,
}

fn default_args() -> serde_json::Value {
    serde_json::json!({})
}

async fn invoke_plugin(
    State(state): State<AdminState>,
    Path(name): Path<String>,
    Json(body): Json<InvokeBody>,
) -> axum::response::Response {
    let Some(entry) = state.plugins.get(&name) else {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "not_found", "resource": "plugin", "id": name})),
        )
            .into_response();
    };

    // Currently only stdio (Sync/Async) plugins go through this path — the
    // service runtime needs a gRPC handle we don't wire up in admin.
    if matches!(entry.manifest.plugin_type, PluginType::Service) {
        return (
            StatusCode::NOT_IMPLEMENTED,
            Json(json!({
                "error": "invoke_unsupported",
                "message": "test-invoke for service plugins is not supported; use the service's own gRPC surface",
            })),
        )
            .into_response();
    }

    // Validate that the tool is actually declared by the manifest.
    let tool_declared = entry
        .manifest
        .capabilities
        .tools
        .iter()
        .any(|t| t.name == body.tool);
    if !tool_declared {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({
                "error": "tool_not_declared",
                "plugin": name,
                "tool": body.tool,
            })),
        )
            .into_response();
    }

    let cwd = entry
        .manifest_path
        .parent()
        .map(std::path::Path::to_path_buf)
        .unwrap_or_else(|| std::path::PathBuf::from("."));

    let args_bytes = match serde_json::to_vec(&body.arguments) {
        Ok(v) => v,
        Err(err) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": "invalid_arguments", "message": err.to_string()})),
            )
                .into_response();
        }
    };

    let timeout_ms = body.timeout_ms.map(|ms| ms.min(60_000));
    let session_key = body.session_key.unwrap_or_else(|| "admin-invoke".into());
    let request_id = format!("admin-invoke-{}", uuid::Uuid::new_v4());
    let trace_id = request_id.clone();

    let result = jsonrpc_execute(
        &name,
        &body.tool,
        &cwd,
        Some(&entry.manifest),
        timeout_ms,
        &args_bytes,
        &session_key,
        &request_id,
        &trace_id,
        None,
        &[],
        CancellationToken::new(),
    )
    .await;

    match result {
        Ok(out) => Json(plugin_output_to_json(out)).into_response(),
        Err(err) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({
                "error": "invoke_failed",
                "message": err.to_string(),
            })),
        )
            .into_response(),
    }
}

fn plugin_output_to_json(out: corlinman_plugins::runtime::PluginOutput) -> serde_json::Value {
    use corlinman_plugins::runtime::PluginOutput;
    match out {
        PluginOutput::Success {
            content,
            duration_ms,
        } => {
            // Try to surface the payload as JSON for readability; fall back
            // to a string preview if it isn't UTF-8/JSON.
            let body = String::from_utf8(content.to_vec()).ok();
            let parsed = body
                .as_deref()
                .and_then(|s| serde_json::from_str::<serde_json::Value>(s).ok());
            json!({
                "status": "success",
                "duration_ms": duration_ms,
                "result": parsed,
                "result_raw": body,
            })
        }
        PluginOutput::Error {
            code,
            message,
            duration_ms,
        } => json!({
            "status": "error",
            "duration_ms": duration_ms,
            "code": code,
            "message": message,
        }),
        PluginOutput::AcceptedForLater {
            task_id,
            duration_ms,
        } => json!({
            "status": "accepted",
            "duration_ms": duration_ms,
            "task_id": task_id,
        }),
    }
}

fn diagnostic_for(plugin: &str, d: &Diagnostic) -> Option<Value> {
    match d {
        Diagnostic::ParseError {
            path,
            origin,
            message,
        } => {
            // Path-based match: the UI wants to see parse failures for plugins
            // whose directory name matches, even though the registry never
            // successfully created an entry for them.
            let matches = path
                .parent()
                .and_then(|p| p.file_name())
                .map(|n| n.to_string_lossy() == plugin)
                .unwrap_or(false);
            matches.then(|| {
                json!({
                    "kind": "parse_error",
                    "path": path.to_string_lossy(),
                    "origin": origin.as_str(),
                    "message": message,
                })
            })
        }
        Diagnostic::NameCollision {
            name,
            winner,
            winner_origin,
            loser,
            loser_origin,
        } => (name == plugin).then(|| {
            json!({
                "kind": "name_collision",
                "winner": winner.to_string_lossy(),
                "winner_origin": winner_origin.as_str(),
                "loser": loser.to_string_lossy(),
                "loser_origin": loser_origin.as_str(),
            })
        }),
    }
}

// ---------------------------------------------------------------------------
// Phase 4 W3 C2 iter 8 — admin mutations against an `McpAdapter`.
//
// Three POST endpoints, each idempotent at the adapter layer:
//
//   POST /admin/plugins/:name/disable  -> { disabled: true }   (sentinel persisted)
//   POST /admin/plugins/:name/enable   -> { disabled: false }  (sentinel removed)
//   POST /admin/plugins/:name/restart  -> { status: "<after>" } (stop+start)
//
// The router is state-disjoint from `AdminState` so it can land in
// the plugin admin module without touching `AdminState` (which is
// out of scope for the C2 worktree). Boot wiring merges this into
// the admin tree alongside the read-only routes once the gateway
// gains an `McpAdapter` field.
// ---------------------------------------------------------------------------

/// Build the iter-8 admin sub-router for MCP-plugin lifecycle
/// mutations. Caller passes an `Arc<McpAdapter>` shared with the
/// chat dispatcher; admin and dispatch share the same instance.
pub fn mcp_admin_router(adapter: Arc<McpAdapter>) -> Router {
    Router::new()
        .route("/admin/plugins/:name/disable", post(disable_mcp_plugin))
        .route("/admin/plugins/:name/enable", post(enable_mcp_plugin))
        .route("/admin/plugins/:name/restart", post(restart_mcp_plugin))
        .with_state(adapter)
}

async fn disable_mcp_plugin(
    State(adapter): State<Arc<McpAdapter>>,
    Path(name): Path<String>,
) -> axum::response::Response {
    match adapter.disable_one(&name).await {
        Ok(()) => Json(json!({
            "name": name,
            "disabled": true,
            "stopped": true,
        }))
        .into_response(),
        Err(err) => adapter_error_response(err),
    }
}

async fn enable_mcp_plugin(
    State(adapter): State<Arc<McpAdapter>>,
    Path(name): Path<String>,
) -> axum::response::Response {
    match adapter.enable_one(&name).await {
        Ok(()) => Json(json!({
            "name": name,
            "disabled": false,
        }))
        .into_response(),
        Err(err) => adapter_error_response(err),
    }
}

async fn restart_mcp_plugin(
    State(adapter): State<Arc<McpAdapter>>,
    Path(name): Path<String>,
) -> axum::response::Response {
    match adapter.restart_one(&name).await {
        Ok(()) => {
            let status = adapter
                .status(&name)
                .await
                .map(|s| s.as_str().to_string())
                .unwrap_or_else(|_| "unknown".into());
            Json(json!({
                "name": name,
                "restarted": true,
                "status": status,
            }))
            .into_response()
        }
        Err(err) => adapter_error_response(err),
    }
}

/// Project [`AdapterError`] onto the admin HTTP error envelope. The
/// status codes follow the convention used by the rest of admin:
///
///   - `UnknownPlugin` → 404 (admin asked for a plugin that isn't
///     registered with the adapter)
///   - `Disabled` → 409 (operation rejected because of a deliberate
///     state — Conflict is the closest standard semantic)
///   - `SentinelIo` → 500 (filesystem can't persist the change; admin
///     should retry or inspect disk)
///   - everything else → 502 (upstream MCP child / handshake / call
///     failure)
fn adapter_error_response(err: AdapterError) -> axum::response::Response {
    let (status, code, message) = match &err {
        AdapterError::UnknownPlugin(name) => (
            StatusCode::NOT_FOUND,
            "plugin_not_found",
            format!("MCP plugin {name:?} is not registered with the adapter"),
        ),
        AdapterError::Disabled(name) => (
            StatusCode::CONFLICT,
            "plugin_disabled",
            format!("MCP plugin {name:?} is administratively disabled"),
        ),
        AdapterError::SentinelIo { plugin, message } => (
            StatusCode::INTERNAL_SERVER_ERROR,
            "sentinel_io_error",
            format!("sentinel I/O for {plugin:?}: {message}"),
        ),
        AdapterError::NotMcpPlugin(name) => (
            StatusCode::BAD_REQUEST,
            "not_mcp_plugin",
            format!("plugin {name:?} is not plugin_type = \"mcp\""),
        ),
        AdapterError::MissingMcpConfig(name) => (
            StatusCode::BAD_REQUEST,
            "missing_mcp_config",
            format!("plugin {name:?} is missing the [mcp] manifest table"),
        ),
        // Spawn / handshake / call failures collapse to 502 so admin
        // sees the upstream-failure banner. The text-of-source chain
        // carries the diagnosable detail.
        other => (
            StatusCode::BAD_GATEWAY,
            "mcp_adapter_error",
            other.to_string(),
        ),
    };
    (
        status,
        Json(json!({
            "error": code,
            "message": message,
        })),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use arc_swap::ArcSwap;
    use axum::body::{to_bytes, Body};
    use axum::http::Request;
    use corlinman_core::config::Config;
    use corlinman_plugins::discovery::{Origin, SearchRoot};
    use corlinman_plugins::registry::PluginRegistry;
    use std::fs;
    use std::sync::Arc;
    use tower::ServiceExt;

    fn manifest_body(name: &str, version: &str) -> String {
        format!(
            "name = \"{name}\"\n\
             version = \"{version}\"\n\
             description = \"scratch plugin\"\n\
             plugin_type = \"sync\"\n\
             [entry_point]\n\
             command = \"true\"\n\
             [[capabilities.tools]]\n\
             name = \"echo\"\n\
             description = \"echo its input\"\n"
        )
    }

    fn scratch_registry() -> (tempfile::TempDir, Arc<PluginRegistry>) {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("alpha");
        fs::create_dir_all(&p).unwrap();
        fs::write(
            p.join("plugin-manifest.toml"),
            manifest_body("alpha", "1.2.3"),
        )
        .unwrap();

        let reg = PluginRegistry::from_roots(vec![SearchRoot::new(dir.path(), Origin::Workspace)]);
        (dir, Arc::new(reg))
    }

    fn app(registry: Arc<PluginRegistry>) -> Router {
        let state = AdminState::new(registry, Arc::new(ArcSwap::from_pointee(Config::default())));
        router(state)
    }

    async fn body_json(resp: axum::response::Response) -> Value {
        let b = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        serde_json::from_slice(&b).unwrap()
    }

    #[tokio::test]
    async fn list_returns_registry_entries() {
        let (_dir, reg) = scratch_registry();
        let resp = app(reg)
            .oneshot(
                Request::builder()
                    .uri("/admin/plugins")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        let arr = v.as_array().unwrap();
        assert_eq!(arr.len(), 1);
        assert_eq!(arr[0]["name"], "alpha");
        assert_eq!(arr[0]["version"], "1.2.3");
        assert_eq!(arr[0]["plugin_type"], "sync");
        assert_eq!(arr[0]["origin"], "workspace");
        assert_eq!(arr[0]["tool_count"], 1);
        assert_eq!(arr[0]["capabilities"], json!(["echo"]));
        assert_eq!(arr[0]["status"], "loaded");
    }

    #[tokio::test]
    async fn detail_returns_manifest_and_summary() {
        let (_dir, reg) = scratch_registry();
        let resp = app(reg)
            .oneshot(
                Request::builder()
                    .uri("/admin/plugins/alpha")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(v["summary"]["name"], "alpha");
        assert_eq!(v["manifest"]["name"], "alpha");
        assert_eq!(v["manifest"]["capabilities"]["tools"][0]["name"], "echo");
        assert!(v["diagnostics"].as_array().unwrap().is_empty());
    }

    #[tokio::test]
    async fn detail_returns_404_for_unknown_plugin() {
        let (_dir, reg) = scratch_registry();
        let resp = app(reg)
            .oneshot(
                Request::builder()
                    .uri("/admin/plugins/nope")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
        let v = body_json(resp).await;
        assert_eq!(v["error"], "not_found");
        assert_eq!(v["id"], "nope");
    }

    #[tokio::test]
    async fn invoke_rejects_unknown_plugin() {
        let (_dir, reg) = scratch_registry();
        let resp = app(reg)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/plugins/nope/invoke")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"tool":"echo"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn invoke_rejects_undeclared_tool() {
        let (_dir, reg) = scratch_registry();
        let resp = app(reg)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/plugins/alpha/invoke")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"tool":"no-such-tool","arguments":{}}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let v = body_json(resp).await;
        assert_eq!(v["error"], "tool_not_declared");
    }

    #[test]
    fn diagnostic_filter_matches_by_dir_name() {
        let d = Diagnostic::ParseError {
            path: "/tmp/plugins/alpha/plugin-manifest.toml".into(),
            origin: Origin::Workspace,
            message: "bad".into(),
        };
        assert!(diagnostic_for("alpha", &d).is_some());
        assert!(diagnostic_for("beta", &d).is_none());
    }

    // ----- Iter 8: MCP admin mutations (disable / enable / restart) -----

    use corlinman_plugins::manifest::{
        AllowlistMode, EntryPoint, EnvPassthrough, McpConfig, PluginManifest, ResourcesAllowlist,
        RestartPolicy, ToolsAllowlist,
    };

    fn mcp_manifest(name: &str, command: &str, args: &[&str]) -> Arc<PluginManifest> {
        Arc::new(PluginManifest {
            manifest_version: 3,
            name: name.into(),
            version: "0.1.0".into(),
            description: String::new(),
            author: String::new(),
            plugin_type: PluginType::Mcp,
            entry_point: EntryPoint {
                command: command.into(),
                args: args.iter().map(|s| s.to_string()).collect(),
                env: Default::default(),
            },
            communication: Default::default(),
            capabilities: Default::default(),
            sandbox: Default::default(),
            mcp: Some(McpConfig {
                autostart: false,
                restart_policy: RestartPolicy::OnCrash,
                crash_loop_max: 3,
                crash_loop_window_secs: 60,
                handshake_timeout_ms: 5_000,
                idle_shutdown_secs: 0,
                env_passthrough: EnvPassthrough {
                    allow: vec![],
                    deny: vec![],
                },
                tools_allowlist: ToolsAllowlist {
                    mode: AllowlistMode::All,
                    names: vec![],
                },
                resources_allowlist: ResourcesAllowlist::default(),
            }),
            meta: None,
            protocols: vec!["openai_function".into()],
            hooks: vec![],
            skill_refs: vec![],
        })
    }

    /// `which`-equivalent check using only `std`: returns true when
    /// `awk` exists on PATH. The plugins crate has the `which` crate
    /// in its dev-deps; rather than pulling that into the gateway
    /// just for a portability skip, we shell out to `command -v`.
    fn awk_available() -> bool {
        std::process::Command::new("sh")
            .arg("-c")
            .arg("command -v awk >/dev/null 2>&1 && command -v sh >/dev/null 2>&1")
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    }

    /// Awk responder identical to the iter-4/5/7 fixtures — minimal
    /// MCP server that echoes initialize / tools/list / tools/call.
    fn awk_responder() -> (&'static str, Vec<String>) {
        let script = r#"awk '
            {
                line=$0
                m = match(line, /"id":[ ]*[0-9]+/)
                if (m == 0) { m = match(line, /"id":[ ]*"[^"]*"/) }
                if (m == 0) { next }
                idstr = substr(line, RSTART+5, RLENGTH-5)
                gsub(/^[ ]+/, "", idstr)
                if (line ~ /"method"[ ]*:[ ]*"initialize"/) {
                    printf "{\"jsonrpc\":\"2.0\",\"id\":%s,\"result\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{\"tools\":{}},\"serverInfo\":{\"name\":\"awk-mcp\",\"version\":\"0.0.1\"}}}\n", idstr
                    fflush()
                }
                else if (line ~ /"method"[ ]*:[ ]*"tools\/list"/) {
                    printf "{\"jsonrpc\":\"2.0\",\"id\":%s,\"result\":{\"tools\":[{\"name\":\"echo\",\"description\":\"\",\"inputSchema\":{\"type\":\"object\"}}]}}\n", idstr
                    fflush()
                }
                else if (line ~ /"method"[ ]*:[ ]*"tools\/call"/) {
                    printf "{\"jsonrpc\":\"2.0\",\"id\":%s,\"result\":{\"content\":[{\"type\":\"text\",\"text\":\"ok\"}],\"isError\":false}}\n", idstr
                    fflush()
                }
            }'"#;
        ("sh", vec!["-c".into(), script.into()])
    }

    /// Helper: build an `Arc<McpAdapter>` with `name` registered + started.
    /// Returns the tempdir so the manifest dir survives the test.
    async fn live_adapter_with(name: &str) -> (tempfile::TempDir, Arc<McpAdapter>) {
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_responder();
        let m = mcp_manifest(
            name,
            cmd,
            &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
        );
        let adapter = Arc::new(McpAdapter::new());
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter.start_one(name).await.unwrap();
        (tmp, adapter)
    }

    #[tokio::test]
    async fn admin_disable_endpoint_returns_disabled_true() {
        if !awk_available() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let (_tmp, adapter) = live_adapter_with("admin-dis").await;
        let app = mcp_admin_router(adapter.clone());
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/plugins/admin-dis/disable")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(v["disabled"], true);
        assert_eq!(v["stopped"], true);
        assert!(adapter.is_disabled("admin-dis").await.unwrap());
    }

    #[tokio::test]
    async fn admin_enable_endpoint_clears_disabled() {
        if !awk_available() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let (_tmp, adapter) = live_adapter_with("admin-ena").await;
        adapter.disable_one("admin-ena").await.unwrap();

        let app = mcp_admin_router(adapter.clone());
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/plugins/admin-ena/enable")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(v["disabled"], false);
        assert!(!adapter.is_disabled("admin-ena").await.unwrap());
    }

    #[tokio::test]
    async fn admin_restart_endpoint_recovers_to_initialized() {
        if !awk_available() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let (_tmp, adapter) = live_adapter_with("admin-rest").await;
        adapter.stop_one("admin-rest").await.unwrap();

        let app = mcp_admin_router(adapter.clone());
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/plugins/admin-rest/restart")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(v["restarted"], true);
        assert_eq!(v["status"], "initialized");
    }

    #[tokio::test]
    async fn admin_disable_endpoint_unknown_returns_404() {
        let adapter = Arc::new(McpAdapter::new());
        let app = mcp_admin_router(adapter);
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/plugins/ghost/disable")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
        let v = body_json(resp).await;
        assert_eq!(v["error"], "plugin_not_found");
    }

    #[tokio::test]
    async fn admin_restart_endpoint_disabled_returns_409() {
        let tmp = tempfile::tempdir().unwrap();
        let m = mcp_manifest("admin-dis-409", "/no-binary", &[]);
        let adapter = Arc::new(McpAdapter::new());
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter.disable_one("admin-dis-409").await.unwrap();

        let app = mcp_admin_router(adapter);
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/plugins/admin-dis-409/restart")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::CONFLICT);
        let v = body_json(resp).await;
        assert_eq!(v["error"], "plugin_disabled");
    }

    #[tokio::test]
    async fn admin_disable_then_enable_round_trip() {
        if !awk_available() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let (_tmp, adapter) = live_adapter_with("admin-rt").await;
        let app = mcp_admin_router(adapter.clone());

        // Disable
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/plugins/admin-rt/disable")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        assert!(adapter.is_disabled("admin-rt").await.unwrap());

        // Enable
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/plugins/admin-rt/enable")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        assert!(!adapter.is_disabled("admin-rt").await.unwrap());
    }
}
