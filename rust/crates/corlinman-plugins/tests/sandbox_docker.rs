//! Docker sandbox integration tests. All `#[ignore]` so they do not run in
//! CI without an explicit opt-in (`cargo test -- --ignored`).
//!
//! Prerequisites for a green run locally:
//!   - Docker daemon reachable via `/var/run/docker.sock`.
//!   - Image `corlinman-sandbox:latest` built from `docker/Dockerfile.sandbox`
//!     (`docker build -t corlinman-sandbox:latest -f docker/Dockerfile.sandbox .`).
//!   - Linux host: macOS / Windows docker desktop works but enforcement of
//!     `readonly_rootfs` can differ.
//!
//! Each test that touches docker pings first; if the daemon is not reachable
//! the test emits a visible `eprintln!` and returns `Ok(())` so `--ignored`
//! sweeps stay green on workstations without docker.

use std::sync::Arc;

use bollard::Docker;
use tokio_util::sync::CancellationToken;

use corlinman_plugins::manifest::{EntryPoint, PluginManifest, PluginType, SandboxConfig};
use corlinman_plugins::runtime::{jsonrpc_stdio, PluginOutput};
use corlinman_plugins::sandbox::{docker::DockerSandbox, DockerRunner, OOM_ERROR_CODE};

async fn docker_available() -> bool {
    match Docker::connect_with_socket_defaults() {
        Ok(d) => d.ping().await.is_ok(),
        Err(_) => false,
    }
}

fn manifest(name: &str, sandbox: SandboxConfig, python_script: &str) -> (PluginManifest, String) {
    let m = PluginManifest {
        manifest_version: 2,
        name: name.into(),
        version: "0.1.0".into(),
        description: String::new(),
        author: String::new(),
        plugin_type: PluginType::Sync,
        entry_point: EntryPoint {
            command: "python3".into(),
            args: vec!["-c".into(), python_script.to_string()],
            env: Default::default(),
        },
        communication: Default::default(),
        capabilities: Default::default(),
        sandbox,
        mcp: None,
        meta: None,
        protocols: vec!["openai_function".into()],
        hooks: vec![],
        skill_refs: vec![],
    };
    (m, python_script.to_string())
}

async fn runner() -> Arc<dyn DockerRunner> {
    Arc::new(DockerSandbox::new().await.expect("DockerSandbox::new"))
}

#[tokio::test]
#[ignore]
async fn sandbox_memory_limit_triggers_oom() {
    if !docker_available().await {
        eprintln!("skipping: docker daemon unreachable");
        return;
    }
    // 64 MiB cap + allocate ~500 MiB → container should be OOM-killed.
    let script = r#"
import sys, json, os
try:
    x = bytearray(500 * 1024 * 1024)
    sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":1,"result":{"alloc_ok":True}}) + "\n")
    sys.stdout.flush()
except MemoryError:
    sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"oom-detected-in-python"}}) + "\n")
    sys.stdout.flush()
"#;
    let sb = SandboxConfig {
        memory: Some("64m".into()),
        cap_drop: vec!["ALL".into()],
        ..Default::default()
    };
    let (m, _) = manifest("oom-probe", sb, script);
    let runner = runner().await;
    let out = jsonrpc_stdio::execute_with_runner(
        &m.name,
        "probe",
        std::path::Path::new("."),
        Some(&m),
        Some(30_000),
        b"{}",
        "sess",
        "req",
        "trace",
        None,
        &[],
        Some(runner),
        CancellationToken::new(),
    )
    .await
    .expect("execute returns");
    match out {
        PluginOutput::Error { code, .. } => {
            // Either kernel-level OOM (our synthetic code) or python-side
            // MemoryError mapped by the plugin — both are acceptable.
            assert!(
                code == OOM_ERROR_CODE || code == -32000,
                "unexpected error code {code}"
            );
        }
        other => panic!("expected Error, got {other:?}"),
    }
}

#[tokio::test]
#[ignore]
async fn sandbox_network_none_blocks_egress() {
    if !docker_available().await {
        eprintln!("skipping: docker daemon unreachable");
        return;
    }
    // network=none → socket() to an external host must fail.
    let script = r#"
import sys, json, socket
try:
    socket.create_connection(("1.1.1.1", 80), timeout=2)
    out = {"jsonrpc":"2.0","id":1,"result":{"egress":"allowed"}}
except OSError as e:
    out = {"jsonrpc":"2.0","id":1,"result":{"egress":"blocked","err":str(e)}}
sys.stdout.write(json.dumps(out) + "\n")
sys.stdout.flush()
"#;
    let sb = SandboxConfig {
        network: Some("none".into()),
        cap_drop: vec!["ALL".into()],
        ..Default::default()
    };
    let (m, _) = manifest("net-probe", sb, script);
    let runner = runner().await;
    let out = jsonrpc_stdio::execute_with_runner(
        &m.name,
        "probe",
        std::path::Path::new("."),
        Some(&m),
        Some(30_000),
        b"{}",
        "sess",
        "req",
        "trace",
        None,
        &[],
        Some(runner),
        CancellationToken::new(),
    )
    .await
    .expect("execute returns");
    match out {
        PluginOutput::Success { content, .. } => {
            let v: serde_json::Value = serde_json::from_slice(&content).unwrap();
            assert_eq!(v["egress"], "blocked", "expected egress blocked, got {v}");
        }
        other => panic!("expected Success, got {other:?}"),
    }
}

#[tokio::test]
#[ignore]
async fn sandbox_readonly_root_blocks_fs_write() {
    if !docker_available().await {
        eprintln!("skipping: docker daemon unreachable");
        return;
    }
    // readonly_rootfs=true → writing /tmp/x must fail; writing to a bind
    // mount must succeed. We rely on docker's default tmpfs-on-/tmp being
    // absent here (no `tmpfs` in HostConfig).
    let bind_src = tempfile::tempdir().expect("tempdir");
    let bind = format!("{}:/mnt/out", bind_src.path().display());
    let script = r#"
import sys, json
root_err = None
mnt_err = None
try:
    with open("/usr/local/x", "w") as f:
        f.write("nope")
except OSError as e:
    root_err = str(e)
try:
    with open("/mnt/out/y", "w") as f:
        f.write("ok")
except OSError as e:
    mnt_err = str(e)
out = {"jsonrpc":"2.0","id":1,"result":{"root_err":root_err,"mnt_err":mnt_err}}
sys.stdout.write(json.dumps(out) + "\n")
sys.stdout.flush()
"#;
    let sb = SandboxConfig {
        read_only_root: true,
        cap_drop: vec!["ALL".into()],
        binds: vec![bind],
        ..Default::default()
    };
    let (m, _) = manifest("ro-probe", sb, script);
    let runner = runner().await;
    let out = jsonrpc_stdio::execute_with_runner(
        &m.name,
        "probe",
        std::path::Path::new("."),
        Some(&m),
        Some(30_000),
        b"{}",
        "sess",
        "req",
        "trace",
        None,
        &[],
        Some(runner),
        CancellationToken::new(),
    )
    .await
    .expect("execute returns");
    match out {
        PluginOutput::Success { content, .. } => {
            let v: serde_json::Value = serde_json::from_slice(&content).unwrap();
            assert!(
                v["root_err"].is_string(),
                "write to readonly root should fail: {v}"
            );
            assert!(
                v["mnt_err"].is_null(),
                "bind-mount write should succeed: {v}"
            );
        }
        other => panic!("expected Success, got {other:?}"),
    }
}
