//! Integration test: with `[evolution.observer.enabled] = true`, emitting
//! a single `tool.call.failed`-equivalent hook event causes the observer
//! to persist exactly one row to `evolution_signals`.
//!
//! We don't drive the gateway HTTP surface here — `evolution_observer::spawn`
//! is the public boot hook, and the wire-up in `main.rs` is just a thin
//! `if cfg.enabled { spawn(...) }` shim. Driving the bus + repo end-to-end
//! is the contract that matters.

use std::sync::Arc;
use std::time::Duration;

use corlinman_core::config::EvolutionObserverConfig;
use corlinman_evolution::{EvolutionStore, SignalsRepo};
use corlinman_gateway::evolution_observer;
use corlinman_hooks::{HookBus, HookEvent};
use tempfile::TempDir;

#[tokio::test]
async fn observer_persists_tool_call_failed_to_evolution_signals() {
    let tmp = TempDir::new().unwrap();
    let db_path = tmp.path().join("evolution.sqlite");
    let store = EvolutionStore::open(&db_path).await.unwrap();
    let repo = SignalsRepo::new(store.pool().clone());

    let bus = Arc::new(HookBus::new(64));
    let cfg = EvolutionObserverConfig {
        enabled: true,
        db_path,
        queue_capacity: 32,
    };
    let _writer = evolution_observer::spawn(bus.clone(), repo.clone(), &cfg);

    bus.emit(HookEvent::ToolCalled {
        tool: "web_search".into(),
        runner_id: "remote-1".into(),
        duration_ms: 17,
        ok: false,
        error_code: Some("disconnected".into()),
    })
    .await
    .expect("emit ok");

    // Poll for the row to land. 1s upper bound is plenty — the writer is
    // a single-tokio-task hop.
    let deadline = tokio::time::Instant::now() + Duration::from_secs(1);
    loop {
        let rows = repo
            .list_since(0, Some("tool.call.failed"), 10)
            .await
            .unwrap();
        if rows.len() == 1 {
            assert_eq!(rows[0].target.as_deref(), Some("web_search"));
            assert_eq!(rows[0].payload_json["error_code"], "disconnected");
            return;
        }
        if tokio::time::Instant::now() >= deadline {
            panic!(
                "expected one tool.call.failed signal in evolution_signals; got {}",
                rows.len()
            );
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
}
