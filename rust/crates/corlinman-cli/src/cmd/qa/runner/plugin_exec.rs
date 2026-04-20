//! `kind: plugin_exec_sync` / `plugin_exec_async` — exercise the JSON-RPC
//! stdio plugin runtime directly.
//!
//! Spawns a tiny throwaway python3 "echo" plugin in a tempdir, feeds the
//! scenario's `arguments` object through
//! [`corlinman_plugins::runtime::jsonrpc_stdio::execute`], and asserts the
//! returned [`PluginOutput`] matches the expected variant.

use std::path::Path;
use std::path::PathBuf;

use corlinman_plugins::manifest::{parse_manifest_file, PluginManifest};
use corlinman_plugins::runtime::{jsonrpc_stdio, PluginOutput};
use tokio_util::sync::CancellationToken;

use crate::cmd::qa::scenario::{JsonContains, PluginExecScenario};

pub async fn run_sync(sc: &PluginExecScenario) -> anyhow::Result<()> {
    let (manifest, cwd, _tmp) = setup_plugin()?;
    let args_json = serde_json::to_vec(&sc.arguments)?;
    let out = jsonrpc_stdio::execute(
        &manifest.name,
        "echo",
        &cwd,
        Some(&manifest),
        None,
        &args_json,
        "qa-session",
        "qa-req",
        "qa-trace",
        None,
        &[],
        CancellationToken::new(),
    )
    .await
    .map_err(|e| anyhow::anyhow!("plugin execute: {e}"))?;

    match out {
        PluginOutput::Success { content, .. } => {
            if !sc.expect.success_json_contains.is_empty() {
                let v: serde_json::Value = serde_json::from_slice(&content)
                    .map_err(|e| anyhow::anyhow!("parse plugin JSON: {e}"))?;
                for jc in &sc.expect.success_json_contains {
                    assert_json(&v, jc)?;
                }
            }
            Ok(())
        }
        other => anyhow::bail!("expected Success, got {other:?}"),
    }
}

pub async fn run_async(sc: &PluginExecScenario) -> anyhow::Result<()> {
    let (manifest, cwd, _tmp) = setup_plugin()?;
    let args_json = serde_json::to_vec(&sc.arguments)?;
    let out = jsonrpc_stdio::execute(
        &manifest.name,
        "echo",
        &cwd,
        Some(&manifest),
        None,
        &args_json,
        "qa-session",
        "qa-req-async",
        "qa-trace-async",
        None,
        &[],
        CancellationToken::new(),
    )
    .await
    .map_err(|e| anyhow::anyhow!("plugin execute: {e}"))?;

    match out {
        PluginOutput::AcceptedForLater { task_id, .. } => {
            if let Some(expected) = &sc.expect.accepted_task_id {
                if &task_id != expected {
                    anyhow::bail!("async task_id mismatch: expected {expected:?} got {task_id:?}");
                }
            }
            Ok(())
        }
        other => anyhow::bail!("expected AcceptedForLater, got {other:?}"),
    }
}

/// Build an in-tempdir python echo plugin with a known behaviour:
///   * `{"mode": "echo", ...}`  ⇒ Success, content = {"echo": <args>}
///   * `{"mode": "task", "task_id": "xxx"}` ⇒ AcceptedForLater(task_id)
///   * `{"mode": "error", "message": "…"}`  ⇒ Error
///
/// The `_tmp` handle must be kept alive by the caller (RAII drop removes
/// the tempdir).
fn setup_plugin() -> anyhow::Result<(PluginManifest, PathBuf, tempfile::TempDir)> {
    let py = which_python().ok_or_else(|| {
        anyhow::anyhow!("python3/python not on PATH; cannot exercise plugin runtime")
    })?;
    let tmp = tempfile::tempdir()?;
    let plugin_dir = tmp.path().join("echo");
    std::fs::create_dir_all(&plugin_dir)?;
    std::fs::write(plugin_dir.join("main.py"), ECHO_SCRIPT)?;

    let manifest_body = format!(
        r#"name = "echo"
version = "0.1.0"
plugin_type = "sync"
[entry_point]
command = "{py}"
args = ["main.py"]
[[capabilities.tools]]
name = "echo"
"#
    );
    let manifest_path = plugin_dir.join("plugin-manifest.toml");
    std::fs::write(&manifest_path, manifest_body)?;
    let manifest = parse_manifest_file(&manifest_path)?;
    Ok((manifest, plugin_dir, tmp))
}

fn which_python() -> Option<String> {
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

const ECHO_SCRIPT: &str = r#"
import json, sys
line = sys.stdin.readline()
if not line:
    sys.exit(2)
req = json.loads(line)
args = (req.get("params") or {}).get("arguments", {}) or {}
mode = args.get("mode", "echo")
resp = {"jsonrpc": "2.0", "id": req.get("id", 1)}
if mode == "error":
    resp["error"] = {"code": -32000, "message": args.get("message", "boom")}
elif mode == "task":
    resp["result"] = {"task_id": args.get("task_id", "task-42")}
else:
    resp["result"] = {"echo": args}
sys.stdout.write(json.dumps(resp))
sys.stdout.write("\n")
sys.stdout.flush()
"#;

fn assert_json(root: &serde_json::Value, jc: &JsonContains) -> anyhow::Result<()> {
    let value = follow_path(root, &jc.path)
        .ok_or_else(|| anyhow::anyhow!("json path {:?} missing", jc.path))?;
    if let Some(substr) = &jc.contains {
        let s = value
            .as_str()
            .map(|v| v.to_string())
            .unwrap_or_else(|| value.to_string());
        if !s.contains(substr) {
            anyhow::bail!(
                "json path {:?} expected to contain {:?}, got {}",
                jc.path,
                substr,
                s
            );
        }
    }
    if let Some(expected) = &jc.equals {
        if value != expected {
            anyhow::bail!(
                "json path {:?} expected equals {}, got {}",
                jc.path,
                expected,
                value
            );
        }
    }
    Ok(())
}

fn follow_path<'a>(root: &'a serde_json::Value, path: &str) -> Option<&'a serde_json::Value> {
    let mut cur = root;
    for seg in path.split('.') {
        if seg.is_empty() {
            return None;
        }
        match cur {
            serde_json::Value::Object(map) => {
                cur = map.get(seg)?;
            }
            serde_json::Value::Array(arr) => {
                let idx: usize = seg.parse().ok()?;
                cur = arr.get(idx)?;
            }
            _ => return None,
        }
    }
    Some(cur)
}
