//! Phase 4 W3 C2 iter 10 — end-to-end MCP plugin round-trip.
//!
//! Drives the full stack:
//!
//!     PluginManifest (v3 with [mcp])
//!       -> McpAdapter::register
//!       -> McpAdapter::start_one  (spawn → initialize → tools/list)
//!       -> McpRuntime::execute    (PluginRuntime ABI)
//!       -> McpAdapter::call_tool  (tools/call multiplex)
//!       -> child fixture (Python or, opt-in, npx)
//!       -> response decode + PluginOutput projection
//!       -> McpAdapter::stop_one    (graceful shutdown)
//!
//! ## Fixture choice
//!
//! Default: a Python MCP echo server in
//! `tests/fixtures/echo_mcp_server.py`. Python is already a CI
//! prerequisite for this crate (`tests/jsonrpc_sync.rs` requires
//! it). The fixture implements `initialize`, `tools/list`,
//! `tools/call`, plus `notifications/initialized` — the minimum
//! the adapter exercises during a complete handshake.
//!
//! Opt-in: set `CORLINMAN_C2_E2E_USE_NPX=1` in the environment to
//! switch to `npx -y @modelcontextprotocol/server-filesystem`.
//! When set without npx on PATH, the test reports skipped rather
//! than failing — keeping CI green even on hosts that opt-in
//! without provisioning the runtime.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use tokio_util::sync::CancellationToken;

use corlinman_plugins::manifest::{
    AllowlistMode, EntryPoint, EnvPassthrough, McpConfig, PluginManifest, PluginType,
    ResourcesAllowlist, RestartPolicy, ToolsAllowlist,
};
use corlinman_plugins::runtime::mcp::adapter::{AdapterStatus, McpAdapter};
use corlinman_plugins::runtime::mcp::McpRuntime;
use corlinman_plugins::runtime::{PluginInput, PluginOutput, PluginRuntime};

/// Resolve `python3` (or `python`) on PATH; returns `None` if neither
/// is available. Mirrors the helper in `tests/jsonrpc_sync.rs`.
fn python_command() -> Option<String> {
    for candidate in ["python3", "python"] {
        if which(candidate).is_some() {
            return Some(candidate.to_string());
        }
    }
    None
}

fn which(bin: &str) -> Option<PathBuf> {
    let path_env = std::env::var("PATH").ok()?;
    for dir in path_env.split(':') {
        let candidate = std::path::Path::new(dir).join(bin);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

/// Build the v3 manifest for the python echo fixture and return
/// `(manifest, manifest_dir)`. The manifest dir is a tempdir;
/// callers must keep the `TempDir` alive for the duration of the
/// test (the adapter holds a `cwd` PathBuf into it).
fn build_python_fixture_manifest() -> Option<(Arc<PluginManifest>, tempfile::TempDir)> {
    let python = python_command()?;
    let tmp = tempfile::tempdir().ok()?;

    // Locate the fixture script — it lives at
    // `tests/fixtures/echo_mcp_server.py` inside this crate.
    let script_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join("echo_mcp_server.py");
    if !script_path.exists() {
        eprintln!("fixture missing: {}", script_path.display());
        return None;
    }

    let manifest = PluginManifest {
        manifest_version: 3,
        name: "echo-mcp".into(),
        version: "0.1.0".into(),
        description: "iter-10 E2E python MCP fixture".into(),
        author: "corlinman-tests".into(),
        plugin_type: PluginType::Mcp,
        entry_point: EntryPoint {
            command: python,
            args: vec![script_path.to_string_lossy().into_owned()],
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
            handshake_timeout_ms: 10_000,
            idle_shutdown_secs: 0,
            env_passthrough: EnvPassthrough {
                allow: vec!["PATH".into(), "HOME".into()],
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
    };
    Some((Arc::new(manifest), tmp))
}

/// Build the optional `@modelcontextprotocol/server-filesystem`
/// manifest. Returns None when `npx` is missing; the test then
/// falls back to the python fixture (if `CORLINMAN_C2_E2E_USE_NPX=1`
/// the test is reported as skipped rather than substituted).
fn build_npx_filesystem_manifest() -> Option<(Arc<PluginManifest>, tempfile::TempDir)> {
    let npx = which("npx")?;
    let tmp = tempfile::tempdir().ok()?;
    // Spawn the filesystem server scoped to the tempdir so the test
    // doesn't read anything outside its own scratch space.
    let manifest = PluginManifest {
        manifest_version: 3,
        name: "fs-mcp".into(),
        version: "0.1.0".into(),
        description: "iter-10 E2E real MCP filesystem server".into(),
        author: "corlinman-tests".into(),
        plugin_type: PluginType::Mcp,
        entry_point: EntryPoint {
            command: npx.to_string_lossy().into_owned(),
            args: vec![
                "-y".into(),
                "@modelcontextprotocol/server-filesystem".into(),
                tmp.path().to_string_lossy().into_owned(),
            ],
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
            // npx -y can take 10+s on a cold cache; give the
            // handshake real headroom.
            handshake_timeout_ms: 60_000,
            idle_shutdown_secs: 0,
            env_passthrough: EnvPassthrough {
                allow: vec!["PATH".into(), "HOME".into(), "USER".into(), "LANG".into()],
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
    };
    Some((Arc::new(manifest), tmp))
}

#[tokio::test]
async fn e2e_full_round_trip_python_fixture() {
    let (manifest, tmp) = match build_python_fixture_manifest() {
        Some(m) => m,
        None => {
            eprintln!("python not on PATH; skipping iter-10 python E2E");
            return;
        }
    };

    let adapter = Arc::new(McpAdapter::new());
    adapter
        .register(manifest, tmp.path().to_path_buf())
        .await
        .expect("register must succeed");

    // Spawn → initialize → tools/list.
    adapter.start_one("echo-mcp").await.expect("start_one");
    assert_eq!(
        adapter.status("echo-mcp").await.unwrap(),
        AdapterStatus::Initialized
    );

    // Tools list reflects the fixture's three tools (echo,
    // read_fixture, always_error). Allowlist mode "all" exports
    // every upstream descriptor.
    let tools = adapter.tools_for("echo-mcp").await.unwrap();
    let names: Vec<&str> = tools.iter().map(|t| t.name.as_str()).collect();
    assert!(names.contains(&"echo"), "expected echo, got {names:?}");
    assert!(
        names.contains(&"read_fixture"),
        "expected read_fixture, got {names:?}"
    );
    assert!(
        names.contains(&"always_error"),
        "expected always_error, got {names:?}"
    );

    // Drive `tools/call` via the PluginRuntime ABI — exactly the
    // path the gateway dispatcher will use once it owns an
    // Arc<McpAdapter>.
    let runtime = McpRuntime::new(Arc::clone(&adapter));

    // 1. Happy path: echo with text payload.
    let input = PluginInput {
        plugin: "echo-mcp".into(),
        tool: "echo".into(),
        args_json: Bytes::from_static(br#"{"text":"hello-iter-10"}"#),
        call_id: "c1".into(),
        session_key: String::new(),
        trace_id: String::new(),
        cwd: tmp.path().to_path_buf(),
        env: Vec::new(),
        deadline_ms: Some(10_000),
    };
    let out = runtime
        .execute(input, None, CancellationToken::new())
        .await
        .expect("echo call must succeed");
    match out {
        PluginOutput::Success { content, .. } => {
            let parsed: serde_json::Value =
                serde_json::from_slice(&content).expect("must be valid JSON");
            assert_eq!(parsed["isError"], false);
            assert_eq!(
                parsed["content"][0]["type"], "text",
                "content[0] type"
            );
            assert_eq!(
                parsed["content"][0]["text"], "echo: hello-iter-10",
                "echo payload mismatch"
            );
        }
        other => panic!("expected Success, got {other:?}"),
    }

    // 2. Real-world tool call: read a file the test wrote into the
    //    fixture's cwd, exercising the read_fixture branch.
    let scratch = tmp.path().join("hello.txt");
    std::fs::write(&scratch, "iter-10 read_fixture marker\n").unwrap();
    let input = PluginInput {
        plugin: "echo-mcp".into(),
        tool: "read_fixture".into(),
        args_json: Bytes::from(
            serde_json::to_vec(&serde_json::json!({"path": scratch.to_string_lossy()})).unwrap(),
        ),
        call_id: "c2".into(),
        session_key: String::new(),
        trace_id: String::new(),
        cwd: tmp.path().to_path_buf(),
        env: Vec::new(),
        deadline_ms: Some(10_000),
    };
    let out = runtime
        .execute(input, None, CancellationToken::new())
        .await
        .expect("read_fixture call must succeed");
    match out {
        PluginOutput::Success { content, .. } => {
            let parsed: serde_json::Value = serde_json::from_slice(&content).unwrap();
            assert_eq!(parsed["isError"], false);
            let body = parsed["content"][0]["text"].as_str().unwrap();
            assert!(
                body.contains("iter-10 read_fixture marker"),
                "unexpected fixture contents: {body}"
            );
        }
        other => panic!("expected Success, got {other:?}"),
    }

    // 3. Error projection: always_error -> PluginOutput::Error.
    let input = PluginInput {
        plugin: "echo-mcp".into(),
        tool: "always_error".into(),
        args_json: Bytes::from_static(b"{}"),
        call_id: "c3".into(),
        session_key: String::new(),
        trace_id: String::new(),
        cwd: tmp.path().to_path_buf(),
        env: Vec::new(),
        deadline_ms: Some(5_000),
    };
    let out = runtime
        .execute(input, None, CancellationToken::new())
        .await
        .expect("call wire must succeed even when isError=true");
    match out {
        PluginOutput::Error { code, message, .. } => {
            assert_eq!(code, -32603);
            assert_eq!(message, "by design");
        }
        other => panic!("expected Error, got {other:?}"),
    }

    // 4. Concurrent calls to confirm multiplexing works against the
    //    real fixture (not just the awk responder).
    let mut handles = Vec::new();
    for i in 0..4 {
        let rt = runtime.clone();
        let cwd = tmp.path().to_path_buf();
        handles.push(tokio::spawn(async move {
            let input = PluginInput {
                plugin: "echo-mcp".into(),
                tool: "echo".into(),
                args_json: Bytes::from(
                    serde_json::to_vec(
                        &serde_json::json!({"text": format!("mux-{i}")}),
                    )
                    .unwrap(),
                ),
                call_id: format!("mux-{i}"),
                session_key: String::new(),
                trace_id: String::new(),
                cwd,
                env: Vec::new(),
                deadline_ms: Some(5_000),
            };
            rt.execute(input, None, CancellationToken::new()).await
        }));
    }
    for (i, h) in handles.into_iter().enumerate() {
        let out = h.await.expect("join").expect("call");
        match out {
            PluginOutput::Success { content, .. } => {
                let parsed: serde_json::Value = serde_json::from_slice(&content).unwrap();
                let text = parsed["content"][0]["text"].as_str().unwrap();
                assert_eq!(text, format!("echo: mux-{i}"));
            }
            other => panic!("expected Success, got {other:?}"),
        }
    }

    // 5. Graceful shutdown.
    adapter.stop_one("echo-mcp").await.expect("stop_one");
    assert_eq!(
        adapter.status("echo-mcp").await.unwrap(),
        AdapterStatus::Stopped
    );
    assert!(!adapter.is_alive("echo-mcp").await.unwrap());
}

/// Optional: real `@modelcontextprotocol/server-filesystem` round-
/// trip. Gated behind `CORLINMAN_C2_E2E_USE_NPX=1` so CI runs
/// without it; opt-in flag triggers a full handshake against the
/// upstream MCP filesystem server, asserting at least the
/// well-known tools (`read_text_file`, `list_directory`, …) appear
/// in the resolved tools surface.
///
/// We deliberately don't pin the exact tool list because the
/// upstream server's surface evolves; the assertion is
/// "we got at least one well-known tool name", which is enough to
/// prove the entire stack — corlinman-plugins client → published
/// MCP server — works end-to-end.
#[tokio::test]
async fn e2e_optional_npx_filesystem_server() {
    if std::env::var("CORLINMAN_C2_E2E_USE_NPX").ok().as_deref() != Some("1") {
        eprintln!(
            "CORLINMAN_C2_E2E_USE_NPX != 1; skipping optional npx round-trip"
        );
        return;
    }
    let (manifest, tmp) = match build_npx_filesystem_manifest() {
        Some(m) => m,
        None => {
            eprintln!("npx not on PATH; skipping optional npx round-trip");
            return;
        }
    };

    let adapter = Arc::new(McpAdapter::new());
    adapter
        .register(manifest, tmp.path().to_path_buf())
        .await
        .expect("register must succeed");

    // npx -y can take a while on a cold cache; the manifest's
    // handshake budget is 60s. Give the test the same headroom.
    let started = std::time::Instant::now();
    adapter
        .start_one("fs-mcp")
        .await
        .expect("start_one against npx must succeed");
    let elapsed = started.elapsed();
    eprintln!("npx handshake completed in {elapsed:?}");

    let tools = adapter.tools_for("fs-mcp").await.unwrap();
    let names: Vec<String> = tools.iter().map(|t| t.name.clone()).collect();
    let known_any = ["read_text_file", "list_directory", "search_files"]
        .iter()
        .any(|t| names.iter().any(|n| n == t));
    assert!(
        known_any,
        "expected at least one well-known filesystem tool, got: {names:?}"
    );

    // Try one tool call against `list_directory` if exposed; some
    // upstream versions name it differently — try a couple of
    // candidates and accept any success.
    let runtime = McpRuntime::new(Arc::clone(&adapter));
    let candidates: &[(&str, serde_json::Value)] = &[
        (
            "list_directory",
            serde_json::json!({"path": tmp.path().to_string_lossy()}),
        ),
        (
            "list_allowed_directories",
            serde_json::json!({}),
        ),
    ];
    let mut hit = false;
    for (tool, args) in candidates {
        if !names.iter().any(|n| n == *tool) {
            continue;
        }
        let input = PluginInput {
            plugin: "fs-mcp".into(),
            tool: (*tool).into(),
            args_json: Bytes::from(serde_json::to_vec(args).unwrap()),
            call_id: format!("fs-{tool}"),
            session_key: String::new(),
            trace_id: String::new(),
            cwd: tmp.path().to_path_buf(),
            env: Vec::new(),
            deadline_ms: Some(15_000),
        };
        let out = runtime
            .execute(input, None, CancellationToken::new())
            .await
            .unwrap_or_else(|err| {
                panic!("call to {tool} failed: {err}");
            });
        if matches!(out, PluginOutput::Success { .. }) {
            hit = true;
            break;
        }
    }
    assert!(hit, "no successful call against any well-known tool");

    // Tear down — npx -y leaves the cached package alive but
    // we want the child process gone before the test ends.
    let stop = tokio::time::timeout(Duration::from_secs(5), adapter.stop_one("fs-mcp")).await;
    assert!(stop.is_ok(), "stop_one timed out");
    assert!(stop.unwrap().is_ok(), "stop_one errored");
}
