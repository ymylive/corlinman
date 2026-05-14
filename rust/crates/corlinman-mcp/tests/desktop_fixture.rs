//! Phase 4 W3 C1 iter 10 — replay a recorded Claude-Desktop session
//! through the live MCP server and assert every server frame matches
//! the fixture shape (modulo `id` + `serverInfo.version`).
//!
//! ## What we lock down
//!
//! The fixture under `tests/fixtures/desktop_2024_11_05.json` carries
//! the canonical 2024-11-05 ordering Claude Desktop exercises:
//!
//!   1. `initialize` (client → server)
//!   2. `initialize` reply (server → client)
//!   3. `notifications/initialized` (client → server)
//!   4. `tools/list` (client → server)
//!   5. `tools/list` reply (server → client)
//!   6. `tools/call` (client → server)
//!   7. `tools/call` reply (server → client)
//!   8. `resources/list` (client → server)
//!   9. `resources/list` reply (server → client)
//!  10. `resources/read` (client → server)
//!  11. `resources/read` reply (server → client)
//!  12. WS close (client → server)
//!
//! For each server reply we deep-compare the recorded JSON against the
//! live frame, with two "drift-tolerated" exclusions:
//!
//!   * top-level `id` (we round-trip whatever the client sent — the
//!     fixture and the live request agree by construction);
//!   * any path listed in the entry's optional `ignore_paths` (e.g.
//!     `result.serverInfo.version`, which tracks `CARGO_PKG_VERSION`).
//!
//! A future real-Desktop capture replaces the synthesised fixture
//! verbatim — the test code stays unchanged.
//!
//! ## Why synthesise the fixture
//!
//! Capturing against a real Desktop binary requires a Desktop install
//! in CI. The synthesised version shapes itself against the same spec
//! Desktop targets (MCP 2024-11-05); when CI gains Desktop the
//! fixture is replaced byte-for-byte and the assertion code unchanged.
//! The shape we lock here is what regressions would actually break.

use std::collections::BTreeMap;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use async_trait::async_trait;
use bytes::Bytes;
use corlinman_mcp::adapters::{ResourcesAdapter, ToolsAdapter};
use corlinman_mcp::server::{
    AdapterDispatcher, FrameHandler, McpServer, McpServerConfig, ServerInfo, TokenAcl,
};
use corlinman_mcp::CapabilityAdapter;
use corlinman_memory_host::MemoryHost;
use corlinman_plugins::registry::PluginRegistry;
use corlinman_plugins::runtime::{PluginInput, PluginOutput, PluginRuntime, ProgressSink};
use corlinman_skills::SkillRegistry;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::Message as TgMessage;
use tokio_util::sync::CancellationToken;

// -------------------------------------------------------------------------
// Stub PluginRuntime that returns `{"results":[]}` for kb:search.
// -------------------------------------------------------------------------

struct KbSearchRuntime;

#[async_trait]
impl PluginRuntime for KbSearchRuntime {
    async fn execute(
        &self,
        _input: PluginInput,
        _progress: Option<Arc<dyn ProgressSink>>,
        _cancel: CancellationToken,
    ) -> Result<PluginOutput, corlinman_core::CorlinmanError> {
        Ok(PluginOutput::success(
            Bytes::from_static(b"{\"results\":[]}"),
            5,
        ))
    }
    fn kind(&self) -> &'static str {
        "kb-search-stub"
    }
}

fn make_kb_registry(tmp: &tempfile::TempDir) -> Arc<PluginRegistry> {
    use std::io::Write;
    let dir = tmp.path().join("kb");
    std::fs::create_dir_all(&dir).unwrap();
    let body = r#"name = "kb"
version = "0.1.0"
plugin_type = "sync"
[entry_point]
command = "true"
[communication]
timeout_ms = 2000
[[capabilities.tools]]
name = "search"
description = "search the kb"
[capabilities.tools.parameters]
type = "object"
"#;
    std::fs::File::create(dir.join("plugin-manifest.toml"))
        .unwrap()
        .write_all(body.as_bytes())
        .unwrap();
    let roots = vec![corlinman_plugins::discovery::SearchRoot::new(
        tmp.path(),
        corlinman_plugins::discovery::Origin::Workspace,
    )];
    Arc::new(PluginRegistry::from_roots(roots))
}

fn make_skills(tmp: &tempfile::TempDir) -> Arc<SkillRegistry> {
    use std::io::Write;
    let mut f = std::fs::File::create(tmp.path().join("summarize.md")).unwrap();
    f.write_all(
        b"---\nname: summarize\ndescription: summarise content\n---\n## summarize\n\nGiven a chunk of text, produce a tight summary.\n",
    )
    .unwrap();
    Arc::new(SkillRegistry::load_from_dir(tmp.path()).expect("skills"))
}

async fn spawn_server(
    plugins: Arc<PluginRegistry>,
    skills: Arc<SkillRegistry>,
) -> (SocketAddr, tokio::task::JoinHandle<()>) {
    let runtime: Arc<dyn PluginRuntime> = Arc::new(KbSearchRuntime);
    let tools =
        Arc::new(ToolsAdapter::with_runtime(plugins, runtime)) as Arc<dyn CapabilityAdapter>;
    let memory_hosts: BTreeMap<String, Arc<dyn MemoryHost>> = BTreeMap::new();
    let resources =
        Arc::new(ResourcesAdapter::new(memory_hosts, skills.clone())) as Arc<dyn CapabilityAdapter>;
    let prompts = Arc::new(corlinman_mcp::adapters::PromptsAdapter::new(skills))
        as Arc<dyn CapabilityAdapter>;
    let dispatcher = AdapterDispatcher::from_adapters(
        ServerInfo {
            name: "corlinman".into(),
            version: env!("CARGO_PKG_VERSION").into(),
        },
        vec![tools, resources, prompts],
    );
    let dispatcher: Arc<dyn FrameHandler> = Arc::new(dispatcher);
    let cfg = McpServerConfig {
        tokens: vec![TokenAcl::permissive("desktop-token")],
        max_frame_bytes: 1_048_576,
    };
    let server = McpServer::new(cfg, dispatcher);
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = server
        .router()
        .into_make_service_with_connect_info::<SocketAddr>();
    let h = tokio::spawn(async move {
        let _ = axum::serve(listener, app).await;
    });
    tokio::task::yield_now().await;
    (addr, h)
}

// -------------------------------------------------------------------------
// Fixture model + comparator
// -------------------------------------------------------------------------

#[derive(Debug, serde::Deserialize)]
struct Fixture {
    #[serde(rename = "fixture_version")]
    _fixture_version: u32,
    #[serde(rename = "description")]
    _description: String,
    exchanges: Vec<Exchange>,
}

#[derive(Debug, serde::Deserialize)]
struct Exchange {
    #[serde(rename = "step")]
    _step: u32,
    label: String,
    direction: String,
    #[serde(default)]
    frame: Value,
    #[serde(default)]
    frame_kind: Option<String>,
    #[serde(default)]
    ignore_paths: Vec<String>,
}

fn fixture_path() -> PathBuf {
    let manifest = std::env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR");
    PathBuf::from(manifest)
        .join("tests")
        .join("fixtures")
        .join("desktop_2024_11_05.json")
}

fn load_fixture() -> Fixture {
    let bytes = std::fs::read(fixture_path()).expect("fixture file present");
    serde_json::from_slice(&bytes).expect("fixture valid JSON")
}

/// Recursively delete every `path` (slash-separated) from the value.
fn redact_paths(mut v: Value, paths: &[String]) -> Value {
    for p in paths {
        let parts: Vec<&str> = p.split('.').collect();
        delete_path(&mut v, &parts);
    }
    v
}

fn delete_path(v: &mut Value, parts: &[&str]) {
    if parts.is_empty() {
        return;
    }
    if parts.len() == 1 {
        if let Value::Object(map) = v {
            map.remove(parts[0]);
        }
        return;
    }
    if let Value::Object(map) = v {
        if let Some(child) = map.get_mut(parts[0]) {
            delete_path(child, &parts[1..]);
        }
    }
}

/// Strip the top-level `id` field (it round-trips between the fixture
/// request and the live reply; we don't lock its concrete value here).
fn strip_id(mut v: Value) -> Value {
    if let Value::Object(map) = &mut v {
        map.remove("id");
    }
    v
}

fn assert_frames_match(label: &str, recorded: &Value, live: &Value, ignore: &[String]) {
    let mut rec = recorded.clone();
    let mut got = live.clone();
    rec = redact_paths(rec, ignore);
    got = redact_paths(got, ignore);
    rec = strip_id(rec);
    got = strip_id(got);
    assert_eq!(
        rec,
        got,
        "fixture mismatch at step '{label}'\nrecorded: {}\nlive:     {}",
        serde_json::to_string_pretty(&rec).unwrap(),
        serde_json::to_string_pretty(&got).unwrap()
    );
}

// -------------------------------------------------------------------------
// Replay test
// -------------------------------------------------------------------------

#[tokio::test]
async fn desktop_fixture_replay_matches_every_server_frame() {
    let plugin_dir = tempfile::tempdir().unwrap();
    let skills_dir = tempfile::tempdir().unwrap();
    let plugins = make_kb_registry(&plugin_dir);
    let skills = make_skills(&skills_dir);

    let (addr, _h) = spawn_server(plugins, skills).await;
    let url = format!("ws://{addr}/mcp?token=desktop-token");
    let (mut ws, resp) = tokio_tungstenite::connect_async(&url).await.unwrap();
    assert_eq!(resp.status().as_u16(), 101);

    let fixture = load_fixture();
    for ex in fixture.exchanges {
        match ex.direction.as_str() {
            "client_to_server" => {
                if ex.frame_kind.as_deref() == Some("ws_close") {
                    ws.close(None).await.unwrap();
                    continue;
                }
                let txt = serde_json::to_string(&ex.frame).unwrap();
                ws.send(TgMessage::Text(txt)).await.unwrap();
            }
            "server_to_client" => {
                let reply = ws.next().await.expect("server frame").expect("ok");
                let text = match reply {
                    TgMessage::Text(t) => t,
                    other => panic!("unexpected ws frame at step '{}': {other:?}", ex.label),
                };
                let live: Value = serde_json::from_str(&text).unwrap();
                assert_frames_match(&ex.label, &ex.frame, &live, &ex.ignore_paths);
            }
            other => panic!("unknown direction in fixture: {other}"),
        }
    }
}

#[test]
fn fixture_loads_and_round_trips_through_serde() {
    // Confirms `tests/fixtures/desktop_2024_11_05.json` is well-formed
    // independent of the server-side replay (so a JSON typo surfaces
    // even if the replay test is skipped).
    let f = load_fixture();
    assert!(!f.exchanges.is_empty(), "fixture must carry exchanges");
    let labels: Vec<_> = f.exchanges.iter().map(|e| e.label.clone()).collect();
    assert!(labels.contains(&"initialize".to_string()));
    assert!(labels.contains(&"resources_read_reply".to_string()));
    assert!(labels.contains(&"close".to_string()));
}

#[test]
fn redact_paths_drops_nested_keys() {
    let v = json!({"result": {"serverInfo": {"name": "corlinman", "version": "0.1.0"}}});
    let stripped = redact_paths(v, &["result.serverInfo.version".to_string()]);
    assert_eq!(
        stripped,
        json!({"result": {"serverInfo": {"name": "corlinman"}}})
    );
}
