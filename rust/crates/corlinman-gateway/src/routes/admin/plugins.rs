//! `GET /admin/plugins` + `GET /admin/plugins/:name`.
//!
//! Read-only views onto the plugin registry. The UI consumes these on the
//! Plugins page (list table → row detail drawer).

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
}
