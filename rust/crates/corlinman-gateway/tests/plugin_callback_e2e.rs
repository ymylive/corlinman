//! End-to-end test for M3 async plugin wiring (roadmap §3 T2).
//!
//! An `async` plugin's `tools/call` reply surfaces as
//! `PluginOutput::AcceptedForLater { task_id }` — the gateway's
//! [`RegistryToolExecutor`] parks on the process-wide [`AsyncTaskRegistry`],
//! and the real result arrives later via `POST /plugin-callback/:task_id`.
//!
//! These tests drive `RegistryToolExecutor::execute` directly (no HTTP layer)
//! — the HTTP route itself has unit coverage in `routes::plugin_callback`.
//! Keeping the E2E at the executor boundary exercises the parking + wakeup
//! path without needing a bound TCP port, so the tests stay hermetic.
//!
//! Coverage:
//!   1. `happy path` — plugin returns `task_id`, test calls
//!      `async_tasks.complete(...)` a short while later, executor resolves with
//!      the callback payload byte-identical to what we delivered.
//!   2. `timeout` — plugin returns `task_id` but no completion arrives;
//!      executor returns `is_error=true, code="timeout"` within the overridden
//!      deadline, and the pending entry is cancelled so late callbacks see
//!      `NotFound`.

use std::sync::Arc;
use std::time::Duration;

use corlinman_agent_client::tool_callback::ToolExecutor;
use corlinman_gateway::routes::chat::RegistryToolExecutor;
use corlinman_plugins::{Origin, PluginRegistry, SearchRoot};
use corlinman_proto::v1::ToolCall as PbToolCall;
use serde_json::{json, Value};

/// Materialise an `async`-type echo plugin under `root`. The script prints a
/// synthetic `{"result": {"task_id": "tsk_echo_e2e"}}` and exits — the real
/// result is delivered via the `AsyncTaskRegistry` by the test itself.
fn scratch_async_plugin(root: &std::path::Path, task_id: &str) {
    let plugin_dir = root.join("echo_async");
    std::fs::create_dir_all(&plugin_dir).unwrap();
    std::fs::write(
        plugin_dir.join("plugin-manifest.toml"),
        r#"
name = "echo_async"
version = "0.1.0"
plugin_type = "async"

[entry_point]
command = "python3"
args = ["main.py"]
"#,
    )
    .unwrap();

    // The plugin reads one JSON-RPC request from stdin, emits a canonical
    // `result.task_id` response (which the stdio runtime translates into
    // `PluginOutput::AcceptedForLater`), then exits.
    let script = format!(
        r#"import json, sys

line = sys.stdin.readline()
req = json.loads(line)
resp = {{
    "jsonrpc": "2.0",
    "id": req.get("id", 1),
    "result": {{"task_id": "{task_id}"}},
}}
sys.stdout.write(json.dumps(resp, separators=(",", ":")))
sys.stdout.write("\n")
sys.stdout.flush()
"#
    );
    std::fs::write(plugin_dir.join("main.py"), script).unwrap();
}

#[tokio::test]
async fn async_plugin_callback_delivers_payload_to_executor() {
    let task_id = "tsk_echo_e2e_ok";
    let tmp = tempfile::tempdir().unwrap();
    scratch_async_plugin(tmp.path(), task_id);

    let registry = Arc::new(PluginRegistry::from_roots(vec![SearchRoot::new(
        tmp.path(),
        Origin::Config,
    )]));
    assert!(registry.get("echo_async").is_some());

    let async_tasks = registry.async_tasks();
    // 2-second ceiling for the executor to wait on the callback. The test
    // completes the task well before that.
    let executor =
        RegistryToolExecutor::new(registry.clone()).with_async_timeout(Duration::from_secs(2));

    let call = PbToolCall {
        call_id: "call_async_ok".into(),
        plugin: "echo_async".into(),
        tool: "greet".into(),
        args_json: br#"{"name":"Ada"}"#.to_vec(),
        seq: 0,
    };

    // Spawn the executor; it will park on the AsyncTaskRegistry waiting for
    // the callback. Kick a separate task to complete after a short delay so
    // we exercise the real wakeup path.
    let executor_handle = tokio::spawn(async move { executor.execute(&call).await });

    // Give the plugin time to spawn + return its task_id, then complete the
    // pending entry. The exact delay isn't load-bearing; any value under the
    // 2s timeout works.
    tokio::time::sleep(Duration::from_millis(200)).await;
    async_tasks
        .complete(task_id, json!({"greeting": "hello Ada"}))
        .expect("complete must find the pending entry");

    let result = executor_handle
        .await
        .expect("executor task joined")
        .expect("executor returned Ok");
    assert!(!result.is_error, "async result must not be an error");
    assert_eq!(result.call_id, "call_async_ok");
    let payload: Value = serde_json::from_slice(&result.result_json).unwrap();
    assert_eq!(
        payload,
        json!({"greeting": "hello Ada"}),
        "payload must be byte-equivalent to the callback JSON"
    );
}

#[tokio::test]
async fn async_plugin_callback_timeout_surfaces_structured_error() {
    let task_id = "tsk_echo_e2e_timeout";
    let tmp = tempfile::tempdir().unwrap();
    scratch_async_plugin(tmp.path(), task_id);

    let registry = Arc::new(PluginRegistry::from_roots(vec![SearchRoot::new(
        tmp.path(),
        Origin::Config,
    )]));
    let async_tasks = registry.async_tasks();

    // Very short timeout so the test finishes quickly when the callback
    // never arrives.
    let executor =
        RegistryToolExecutor::new(registry.clone()).with_async_timeout(Duration::from_millis(300));

    let call = PbToolCall {
        call_id: "call_async_to".into(),
        plugin: "echo_async".into(),
        tool: "greet".into(),
        args_json: br#"{}"#.to_vec(),
        seq: 0,
    };

    let result = executor.execute(&call).await.expect("executor returned Ok");
    assert!(
        result.is_error,
        "timeout must produce is_error=true, got {result:?}"
    );
    let payload: Value = serde_json::from_slice(&result.result_json).unwrap();
    assert_eq!(payload["code"], "timeout");
    assert_eq!(payload["task_id"], task_id);

    // After timeout the pending entry must be cancelled so a late callback
    // observes NotFound instead of racing into a dropped sender.
    assert!(
        !async_tasks.is_pending(task_id),
        "timeout must cancel the pending entry"
    );
}
