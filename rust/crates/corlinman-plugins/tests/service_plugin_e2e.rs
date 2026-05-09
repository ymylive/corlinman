//! End-to-end test for the service-plugin stack (Sprint 2 T1).
//!
//! Scenario:
//!   1. write a Python gRPC plugin that binds `$CORLINMAN_PLUGIN_ADDR` (UDS)
//!      and implements `PluginBridge.Execute` returning a canned result.
//!   2. spawn it under [`PluginSupervisor`], dial it with [`ServiceRuntime`],
//!      and fire two concurrent tool calls — assert both succeed.
//!   3. SIGKILL the child; assert the watchdog respawns it and a third tool
//!      call succeeds against the fresh process.
//!
//! The test relies on the repo's `.venv` Python (which already has `grpcio`
//! plus the compiled `corlinman_grpc` proto bindings). When neither `.venv`
//! nor system grpc is available the test returns early with a log message.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use tokio_util::sync::CancellationToken;

use corlinman_plugins::manifest::{EntryPoint, PluginManifest, PluginType};
use corlinman_plugins::runtime::service_grpc::ServiceRuntime;
use corlinman_plugins::runtime::{PluginInput, PluginOutput};
use corlinman_plugins::PluginSupervisor;

/// Locate a Python interpreter that has `grpcio` and the generated
/// `corlinman_grpc` bindings on `sys.path`. Returns the interpreter path plus
/// any extra PYTHONPATH entries the child needs.
fn resolve_python() -> Option<(PathBuf, Option<PathBuf>)> {
    // 1. Prefer the checked-in .venv under the repo root.
    let repo_root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3) // crate -> crates -> rust -> repo
        .map(Path::to_path_buf);
    if let Some(root) = repo_root.as_ref() {
        let venv_python = root.join(".venv").join("bin").join("python");
        if venv_python.is_file() {
            return Some((venv_python, None));
        }
    }

    // 2. Fall back to python3 on PATH; require the user to have installed
    //    grpcio + the generated bindings (via PYTHONPATH).
    for candidate in ["python3", "python"] {
        if let Some(bin) = which(candidate) {
            let pythonpath = repo_root.as_ref().map(|r| {
                r.join("python")
                    .join("packages")
                    .join("corlinman-grpc")
                    .join("src")
            });
            // Sniff whether this interpreter actually has grpc installed.
            let mut cmd = std::process::Command::new(&bin);
            cmd.arg("-c").arg("import grpc, grpc.aio");
            if cmd.status().map(|s| s.success()).unwrap_or(false) {
                return Some((bin, pythonpath));
            }
        }
    }
    None
}

fn which(bin: &str) -> Option<PathBuf> {
    let path_env = std::env::var("PATH").ok()?;
    for dir in path_env.split(':') {
        let candidate = Path::new(dir).join(bin);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

/// Python gRPC plugin: binds UDS on `CORLINMAN_PLUGIN_ADDR`, implements
/// `PluginBridge.Execute` returning a `PluginToolResult` whose body echoes
/// the plugin's PID so the test can tell respawns apart.
const GRPC_PLUGIN: &str = r#"
import asyncio, json, os, sys, signal
import grpc
from grpc import aio
from corlinman_grpc._generated.corlinman.v1 import plugin_pb2, plugin_pb2_grpc


class Bridge(plugin_pb2_grpc.PluginBridgeServicer):
    async def Execute(self, request, context):
        payload = {
            "pid": os.getpid(),
            "call_id": request.call_id,
            "plugin": request.plugin,
            "tool": request.tool,
            "args_len": len(request.args_json),
        }
        yield plugin_pb2.ToolEvent(
            result=plugin_pb2.PluginToolResult(
                call_id=request.call_id,
                result_json=json.dumps(payload).encode("utf-8"),
                duration_ms=1,
            )
        )


async def main():
    addr = os.environ.get("CORLINMAN_PLUGIN_ADDR")
    if not addr:
        sys.stderr.write("missing CORLINMAN_PLUGIN_ADDR\n")
        sys.exit(2)
    server = aio.server()
    plugin_pb2_grpc.add_PluginBridgeServicer_to_server(Bridge(), server)
    server.add_insecure_port(f"unix:{addr}")
    await server.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(s, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    await server.stop(0.5)


if __name__ == "__main__":
    asyncio.run(main())
"#;

fn write_plugin(dir: &Path, python_bin: &Path, extra_pythonpath: Option<&Path>) -> PluginManifest {
    std::fs::create_dir_all(dir).unwrap();
    let script = dir.join("service.py");
    std::fs::write(&script, GRPC_PLUGIN).unwrap();

    let mut env = std::collections::BTreeMap::new();
    if let Some(extra) = extra_pythonpath {
        env.insert(
            "PYTHONPATH".to_string(),
            extra.to_string_lossy().into_owned(),
        );
    }

    PluginManifest {
        manifest_version: 2,
        name: "e2e_service".into(),
        version: "0.1.0".into(),
        description: String::new(),
        author: String::new(),
        plugin_type: PluginType::Service,
        entry_point: EntryPoint {
            command: python_bin.to_string_lossy().into_owned(),
            args: vec![script.to_string_lossy().into_owned()],
            env,
        },
        communication: Default::default(),
        capabilities: Default::default(),
        sandbox: Default::default(),
        mcp: None,
        meta: None,
        protocols: vec!["openai_function".into()],
        hooks: vec![],
        skill_refs: vec![],
    }
}

fn make_input(call_id: &str, plugin: &str) -> PluginInput {
    PluginInput {
        plugin: plugin.to_string(),
        tool: "echo".into(),
        args_json: Bytes::from_static(b"{}"),
        call_id: call_id.to_string(),
        session_key: String::new(),
        trace_id: String::new(),
        cwd: PathBuf::from("/tmp"),
        env: Vec::new(),
        deadline_ms: Some(5_000),
    }
}

/// Extract the child PID from the plugin's JSON response, so we can tell
/// the pre-restart and post-restart processes apart.
fn pid_from_output(out: &PluginOutput) -> i64 {
    match out {
        PluginOutput::Success { content, .. } => {
            let v: serde_json::Value = serde_json::from_slice(content).expect("valid json");
            v.get("pid").and_then(|p| p.as_i64()).expect("pid field")
        }
        other => panic!("expected success, got: {other:?}"),
    }
}

#[tokio::test]
async fn service_plugin_concurrent_then_respawn() {
    let Some((python_bin, extra_pythonpath)) = resolve_python() else {
        eprintln!("skipping: no grpc-capable python interpreter found");
        return;
    };

    let workdir = tempfile::tempdir().expect("tempdir");
    let plugin_dir = workdir.path().join("plugin");
    let socket_root = workdir.path().join("sockets");
    std::fs::create_dir_all(&socket_root).unwrap();

    let manifest = write_plugin(&plugin_dir, &python_bin, extra_pythonpath.as_deref());

    let runtime = Arc::new(ServiceRuntime::new());
    let supervisor = Arc::new(PluginSupervisor::new(socket_root.clone()));

    let socket = supervisor
        .spawn_service(&manifest)
        .await
        .expect("spawn_service");
    runtime
        .register(&manifest.name, &socket)
        .await
        .expect("register");
    Arc::clone(&supervisor).start_watchdog(
        manifest.name.clone(),
        manifest.clone(),
        Arc::clone(&runtime),
    );

    // --- Phase 1: two concurrent tool calls, both succeed ---
    let (a, b) = tokio::join!(
        runtime.execute(make_input("c-a", &manifest.name), CancellationToken::new()),
        runtime.execute(make_input("c-b", &manifest.name), CancellationToken::new()),
    );
    let out_a = a.expect("call a");
    let out_b = b.expect("call b");
    let pid_before = pid_from_output(&out_a);
    assert_eq!(pid_before, pid_from_output(&out_b), "same child pid");

    // --- Phase 2: kill child, wait for watchdog respawn, third call succeeds ---
    let kill_status = std::process::Command::new("kill")
        .arg("-9")
        .arg(pid_before.to_string())
        .status()
        .expect("kill");
    assert!(kill_status.success(), "kill succeeded");

    // Wait up to 10s for the watchdog to respawn and re-register. Poll every
    // 150ms by attempting a fresh tool call.
    let mut pid_after: Option<i64> = None;
    for _ in 0..70 {
        tokio::time::sleep(Duration::from_millis(150)).await;
        match runtime
            .execute(make_input("c-c", &manifest.name), CancellationToken::new())
            .await
        {
            Ok(out) => {
                let pid = pid_from_output(&out);
                if pid != pid_before {
                    pid_after = Some(pid);
                    break;
                }
            }
            Err(_) => {
                // Client still pointing at dead channel; watchdog hasn't
                // re-registered yet — keep polling.
            }
        }
    }
    let pid_after = pid_after.expect("watchdog respawned within 10s");
    assert_ne!(pid_after, pid_before, "new pid after respawn");

    // --- Cleanup ---
    supervisor.shutdown().await;
}
