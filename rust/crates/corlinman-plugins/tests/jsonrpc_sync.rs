//! End-to-end tests for the JSON-RPC 2.0 stdio runtime.
//!
//! Each test spawns a tiny Python "echo" plugin written to a `tempdir`.
//! Python is assumed to be on PATH in CI; if it's not, `which python3` fails
//! and the tests are skipped with an explanatory message.

use std::path::{Path, PathBuf};

use bytes::Bytes;
use tokio_util::sync::CancellationToken;

use corlinman_core::CorlinmanError;
use corlinman_plugins::manifest::{parse_manifest_file, PluginManifest};
use corlinman_plugins::runtime::{jsonrpc_stdio, PluginOutput};

fn python_available() -> Option<String> {
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
        let candidate = Path::new(dir).join(bin);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

/// Minimal Python echo plugin. Reads one JSON-RPC request from stdin and
/// emits exactly one response line on stdout.
const ECHO_PLUGIN: &str = r#"
import json, sys
line = sys.stdin.readline()
if not line:
    sys.exit(2)
req = json.loads(line)
params = req.get("params", {}) or {}
name = params.get("name", "")
args = params.get("arguments", {}) or {}
mode = args.get("mode", "echo")
resp = {"jsonrpc": "2.0", "id": req.get("id", 1)}
if mode == "error":
    resp["error"] = {"code": -32000, "message": args.get("message", "boom")}
elif mode == "task":
    resp["result"] = {"task_id": args.get("task_id", "task-123")}
elif mode == "sleep":
    import time
    time.sleep(float(args.get("seconds", 5)))
    resp["result"] = {"slept": args.get("seconds", 5)}
elif mode == "malformed":
    sys.stdout.write("not-json\n")
    sys.stdout.flush()
    sys.exit(0)
else:
    resp["result"] = {"name": name, "echo": args}
sys.stdout.write(json.dumps(resp) + "\n")
sys.stdout.flush()
"#;

fn make_echo_plugin(tmp: &Path, python_cmd: &str, timeout_ms: Option<u64>) -> PluginManifest {
    let dir = tmp.join("echo");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("main.py"), ECHO_PLUGIN).unwrap();
    let mut manifest_body = String::new();
    manifest_body.push_str("name = \"echo\"\n");
    manifest_body.push_str("version = \"0.1.0\"\n");
    manifest_body.push_str("plugin_type = \"sync\"\n");
    manifest_body.push_str("[entry_point]\n");
    manifest_body.push_str(&format!("command = \"{python_cmd}\"\n"));
    manifest_body.push_str("args = [\"main.py\"]\n");
    if let Some(ms) = timeout_ms {
        manifest_body.push_str("[communication]\n");
        manifest_body.push_str(&format!("timeout_ms = {ms}\n"));
    }
    manifest_body.push_str("[[capabilities.tools]]\n");
    manifest_body.push_str("name = \"echo\"\n");
    let path = dir.join("plugin-manifest.toml");
    std::fs::write(&path, manifest_body).unwrap();
    parse_manifest_file(&path).unwrap()
}

async fn run(
    manifest: &PluginManifest,
    cwd: &Path,
    args_json: &[u8],
    cancel: CancellationToken,
) -> Result<PluginOutput, CorlinmanError> {
    jsonrpc_stdio::execute(
        &manifest.name,
        "echo",
        cwd,
        Some(manifest),
        None,
        args_json,
        "test-session",
        "req-1",
        "trace-1",
        None,
        &[],
        cancel,
    )
    .await
}

#[tokio::test]
async fn success_echo() {
    let Some(py) = python_available() else {
        eprintln!("skipping: python3 not on PATH");
        return;
    };
    let tmp = tempfile::tempdir().unwrap();
    let manifest = make_echo_plugin(tmp.path(), &py, None);
    let cwd = tmp.path().join("echo");
    let args = br#"{"mode":"echo","hello":"world"}"#;
    let out = run(&manifest, &cwd, args, CancellationToken::new())
        .await
        .expect("execute");
    match out {
        PluginOutput::Success { content, .. } => {
            let v: serde_json::Value = serde_json::from_slice(&content).unwrap();
            assert_eq!(v["echo"]["hello"], "world");
        }
        other => panic!("expected Success, got {other:?}"),
    }
}

#[tokio::test]
async fn error_maps_to_plugin_error() {
    let Some(py) = python_available() else {
        eprintln!("skipping: python3 not on PATH");
        return;
    };
    let tmp = tempfile::tempdir().unwrap();
    let manifest = make_echo_plugin(tmp.path(), &py, None);
    let cwd = tmp.path().join("echo");
    let args = br#"{"mode":"error","message":"nope"}"#;
    let out = run(&manifest, &cwd, args, CancellationToken::new())
        .await
        .expect("execute");
    match out {
        PluginOutput::Error { code, message, .. } => {
            assert_eq!(code, -32000);
            assert_eq!(message, "nope");
        }
        other => panic!("expected Error, got {other:?}"),
    }
}

#[tokio::test]
async fn async_task_id_yields_accepted() {
    let Some(py) = python_available() else {
        eprintln!("skipping: python3 not on PATH");
        return;
    };
    let tmp = tempfile::tempdir().unwrap();
    let manifest = make_echo_plugin(tmp.path(), &py, None);
    let cwd = tmp.path().join("echo");
    let args = br#"{"mode":"task","task_id":"abc-123"}"#;
    let out = run(&manifest, &cwd, args, CancellationToken::new())
        .await
        .expect("execute");
    match out {
        PluginOutput::AcceptedForLater { task_id, .. } => {
            assert_eq!(task_id, "abc-123");
        }
        other => panic!("expected AcceptedForLater, got {other:?}"),
    }
}

#[tokio::test]
async fn timeout_is_reported() {
    let Some(py) = python_available() else {
        eprintln!("skipping: python3 not on PATH");
        return;
    };
    let tmp = tempfile::tempdir().unwrap();
    // 100ms manifest timeout; plugin sleeps 5s.
    let manifest = make_echo_plugin(tmp.path(), &py, Some(100));
    let cwd = tmp.path().join("echo");
    let args = br#"{"mode":"sleep","seconds":5}"#;
    let err = run(&manifest, &cwd, args, CancellationToken::new())
        .await
        .unwrap_err();
    match err {
        CorlinmanError::Timeout { millis, .. } => assert_eq!(millis, 100),
        other => panic!("expected Timeout, got {other:?}"),
    }
}

#[tokio::test]
async fn cancel_token_short_circuits() {
    let Some(py) = python_available() else {
        eprintln!("skipping: python3 not on PATH");
        return;
    };
    let tmp = tempfile::tempdir().unwrap();
    let manifest = make_echo_plugin(tmp.path(), &py, Some(10_000));
    let cwd = tmp.path().join("echo");
    let args = br#"{"mode":"sleep","seconds":5}"#;
    let cancel = CancellationToken::new();
    let trigger = cancel.clone();
    tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        trigger.cancel();
    });
    let err = run(&manifest, &cwd, args, cancel).await.unwrap_err();
    assert!(
        matches!(err, CorlinmanError::Cancelled(_)),
        "expected Cancelled, got {err:?}"
    );
}

#[tokio::test]
async fn malformed_response_is_parse_error() {
    let Some(py) = python_available() else {
        eprintln!("skipping: python3 not on PATH");
        return;
    };
    let tmp = tempfile::tempdir().unwrap();
    let manifest = make_echo_plugin(tmp.path(), &py, None);
    let cwd = tmp.path().join("echo");
    let args = br#"{"mode":"malformed"}"#;
    let err = run(&manifest, &cwd, args, CancellationToken::new())
        .await
        .unwrap_err();
    match err {
        CorlinmanError::Parse { what, .. } => assert_eq!(what, "jsonrpc_stdio:response"),
        other => panic!("expected Parse, got {other:?}"),
    }
}

#[tokio::test]
async fn empty_args_become_empty_object() {
    // Unit-level: request parsing must tolerate empty byte slice → {}.
    let Some(py) = python_available() else {
        eprintln!("skipping: python3 not on PATH");
        return;
    };
    let tmp = tempfile::tempdir().unwrap();
    let manifest = make_echo_plugin(tmp.path(), &py, None);
    let cwd = tmp.path().join("echo");
    let out = run(&manifest, &cwd, b"", CancellationToken::new())
        .await
        .expect("execute");
    if let PluginOutput::Success { content, .. } = out {
        let v: serde_json::Value = serde_json::from_slice(&content).unwrap();
        assert!(v["echo"].is_object());
    } else {
        panic!("expected Success, got {:?}", Bytes::new());
    }
}
