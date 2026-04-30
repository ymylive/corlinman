//! `EvolutionObserver` — Phase 2 wave 1-A.
//!
//! Subscribes to the shared [`corlinman_hooks::HookBus`], adapts a curated
//! subset of [`HookEvent`] variants into [`EvolutionSignal`]s, and persists
//! them via [`SignalsRepo`]. The full design lives in
//! `docs/design/auto-evolution.md` §4.1.
//!
//! ### Adapter mapping
//!
//! The design doc lists abstract event names that pre-date the `HookBus`'s
//! own enum; the observer's `adapt` function is the bridge:
//!
//! | Design name        | Hook variant                                                         |
//! |--------------------|----------------------------------------------------------------------|
//! | `tool.call.failed` | `HookEvent::ToolCalled` with `ok = false` and `error_code != "timeout"` |
//! | `tool.call.timeout`| `HookEvent::ToolCalled` with `ok = false` and `error_code = "timeout"`  |
//! | `approval.rejected`| `HookEvent::ApprovalDecided` with `decision != "allow"` (deny/timeout)  |
//! | `session.ended`    | *no equivalent on the bus today — skipped*                              |
//!
//! `session.ended` will land once the session lifecycle gains a terminal
//! event; the adapter's `adapt` returns `None` for any other variant so
//! adding new mappings later is purely additive.
//!
//! ### Backpressure
//!
//! The hook subscription drains into a bounded `tokio::sync::mpsc` channel
//! sized by [`EvolutionObserverConfig::queue_capacity`]. When the channel
//! is full new events evict the *oldest* queued row (so the freshest
//! context is what gets persisted on a sustained burst) and bump
//! `gateway_evolution_signals_dropped_total`.

use std::sync::Arc;

use corlinman_core::config::EvolutionObserverConfig;
use corlinman_core::metrics::{
    EVOLUTION_SIGNALS_DROPPED, EVOLUTION_SIGNALS_OBSERVED, EVOLUTION_SIGNALS_QUEUE_DEPTH,
};
use corlinman_evolution::{EvolutionSignal, SignalSeverity, SignalsRepo};
use corlinman_hooks::{HookBus, HookEvent, HookPriority};
use serde_json::json;
use tokio::sync::{mpsc, Mutex};
use tokio::task::JoinHandle;

/// Spawn the observer. Returns a join handle for the background writer
/// task. The observer holds an internal `mpsc::Sender` to a bounded queue;
/// the subscriber loop forwards adapted signals into it.
///
/// On bus shutdown (the `HookBus`'s broadcast channel reports `Closed`)
/// the subscriber loop exits, the channel is dropped, and the writer task
/// drains any remaining queued signals before returning.
pub fn spawn(
    bus: Arc<HookBus>,
    repo: SignalsRepo,
    cfg: &EvolutionObserverConfig,
) -> JoinHandle<()> {
    let capacity = cfg.queue_capacity.max(1);
    let (tx, rx) = mpsc::channel::<EvolutionSignal>(capacity);
    // Wrap the sender in a `Mutex` so the subscriber can implement
    // "drop oldest on overflow": when `try_send` returns `Full` we
    // pop one entry from the *receiver* side to free a slot, then retry.
    // The receiver lives behind the same mutex so the writer task and
    // the eviction path don't race on it.
    let rx = Arc::new(Mutex::new(rx));

    // Subscriber loop: read from the bus, adapt, push into the queue.
    {
        let tx = tx.clone();
        let rx = rx.clone();
        let bus = bus.clone();
        tokio::spawn(async move {
            let mut sub = bus.subscribe(HookPriority::Low);
            loop {
                match sub.recv().await {
                    Ok(event) => {
                        let Some(signal) = adapt(&event) else {
                            continue;
                        };
                        enqueue_with_eviction(&tx, &rx, signal).await;
                    }
                    Err(corlinman_hooks::RecvError::Lagged(n)) => {
                        // The Low tier broadcast channel ran ahead of us; we
                        // missed `n` events. Count them as drops so dashboards
                        // see the same backpressure signal as queue overflow.
                        tracing::warn!(
                            lagged = n,
                            "evolution observer lagged hook bus; counted as drops"
                        );
                        EVOLUTION_SIGNALS_DROPPED.inc_by(n as f64);
                    }
                    Err(corlinman_hooks::RecvError::Closed) => {
                        tracing::debug!("evolution observer subscriber closed");
                        break;
                    }
                }
            }
            // Subscription closed; drop our `tx` clone so the writer task
            // sees the channel close once every other clone is dropped.
            drop(tx);
        });
    }

    // Writer task: drain the bounded queue into SignalsRepo. Lives as long
    // as any sender holds a clone of `tx`; exits cleanly when all senders
    // are dropped (i.e. the subscriber loop above terminated).
    let writer = {
        let rx = rx.clone();
        let repo = repo.clone();
        tokio::spawn(async move {
            loop {
                let next = {
                    let mut guard = rx.lock().await;
                    guard.recv().await
                };
                let Some(signal) = next else {
                    tracing::debug!("evolution observer writer drained; exiting");
                    break;
                };
                EVOLUTION_SIGNALS_QUEUE_DEPTH.dec();
                let event_kind = signal.event_kind.clone();
                let severity = signal.severity.as_str();
                match repo.insert(&signal).await {
                    Ok(_) => {
                        EVOLUTION_SIGNALS_OBSERVED
                            .with_label_values(&[event_kind.as_str(), severity])
                            .inc();
                    }
                    Err(err) => {
                        tracing::warn!(
                            error = %err,
                            event_kind = %event_kind,
                            "evolution observer write failed; dropping signal"
                        );
                        EVOLUTION_SIGNALS_DROPPED.inc();
                    }
                }
            }
        })
    };

    // The subscriber's `tx` keeps the channel open; we explicitly drop the
    // local clone so only the subscriber loop holds a sender. When that
    // loop exits the writer drains and returns.
    drop(tx);
    writer
}

/// Adapt a [`HookEvent`] into an [`EvolutionSignal`]. Returns `None` for
/// events we don't track. See module docs for the mapping.
pub(crate) fn adapt(event: &HookEvent) -> Option<EvolutionSignal> {
    match event {
        HookEvent::ToolCalled {
            tool,
            runner_id,
            duration_ms,
            ok,
            error_code,
            tenant_id,
        } => {
            if *ok {
                return None;
            }
            let is_timeout = error_code.as_deref() == Some("timeout");
            let event_kind = if is_timeout {
                "tool.call.timeout"
            } else {
                "tool.call.failed"
            };
            let severity = if is_timeout {
                SignalSeverity::Warn
            } else {
                SignalSeverity::Error
            };
            let payload = json!({
                "tool": tool,
                "runner_id": runner_id,
                "duration_ms": duration_ms,
                "ok": ok,
                "error_code": error_code,
            });
            Some(EvolutionSignal {
                id: None,
                event_kind: event_kind.into(),
                target: Some(tool.clone()),
                severity,
                payload_json: payload,
                trace_id: None,
                session_id: None,
                observed_at: now_ms(),
                tenant_id: tenant_id.clone().unwrap_or_else(|| "default".into()),
            })
        }
        HookEvent::ApprovalDecided {
            id,
            decision,
            decider,
            decided_at_ms,
            tenant_id,
        } => {
            // Only non-allow decisions seed the EvolutionLoop; allow is
            // the happy path and would just pollute clusters.
            if decision == "allow" {
                return None;
            }
            let payload = json!({
                "id": id,
                "decision": decision,
                "decider": decider,
                "decided_at_ms": decided_at_ms,
            });
            Some(EvolutionSignal {
                id: None,
                event_kind: "approval.rejected".into(),
                target: Some(id.clone()),
                severity: SignalSeverity::Warn,
                payload_json: payload,
                trace_id: None,
                session_id: None,
                observed_at: now_ms(),
                tenant_id: tenant_id.clone().unwrap_or_else(|| "default".into()),
            })
        }
        // Phase 2 wave 2-B closed loop: scheduler-driven engine runs
        // emit completion events. We persist them as low-severity
        // signals so the *next* engine run sees its own predecessor's
        // outcome — useful for spotting consecutive failures or
        // proposal-generation regressions.
        HookEvent::EngineRunCompleted {
            run_id,
            proposals_generated,
            duration_ms,
        } => {
            let payload = json!({
                "run_id": run_id,
                "proposals_generated": proposals_generated,
                "duration_ms": duration_ms,
            });
            Some(EvolutionSignal {
                id: None,
                event_kind: "engine.run.completed".into(),
                target: Some(run_id.clone()),
                severity: SignalSeverity::Info,
                payload_json: payload,
                trace_id: None,
                session_id: None,
                observed_at: now_ms(),
                tenant_id: "default".into(),
            })
        }
        HookEvent::EngineRunFailed {
            run_id,
            error_kind,
            exit_code,
        } => {
            let payload = json!({
                "run_id": run_id,
                "error_kind": error_kind,
                "exit_code": exit_code,
            });
            Some(EvolutionSignal {
                id: None,
                event_kind: "engine.run.failed".into(),
                target: Some(run_id.clone()),
                severity: SignalSeverity::Error,
                payload_json: payload,
                trace_id: None,
                session_id: None,
                observed_at: now_ms(),
                tenant_id: "default".into(),
            })
        }
        // Other variants are not part of the curated set today. New
        // mappings (e.g. `session.ended` once the lifecycle gains a
        // terminal event, or `user.correction` once that hook lands) drop
        // in here additively.
        _ => None,
    }
}

/// Push a signal into the bounded queue, evicting the *oldest* entry on
/// overflow. The eviction path takes the receiver lock briefly so the
/// writer task can't race in between.
async fn enqueue_with_eviction(
    tx: &mpsc::Sender<EvolutionSignal>,
    rx: &Arc<Mutex<mpsc::Receiver<EvolutionSignal>>>,
    signal: EvolutionSignal,
) {
    let mut payload = signal;
    loop {
        match tx.try_send(payload) {
            Ok(()) => {
                EVOLUTION_SIGNALS_QUEUE_DEPTH.inc();
                return;
            }
            Err(mpsc::error::TrySendError::Full(rejected)) => {
                tracing::warn!("evolution observer queue full, dropping oldest");
                EVOLUTION_SIGNALS_DROPPED.inc();
                {
                    let mut guard = rx.lock().await;
                    if guard.try_recv().is_ok() {
                        EVOLUTION_SIGNALS_QUEUE_DEPTH.dec();
                    }
                }
                payload = rejected;
                // Loop and retry the send; if the writer happened to pull
                // an item between the eviction and the retry, the second
                // `try_send` succeeds without the loop iterating again.
            }
            Err(mpsc::error::TrySendError::Closed(_)) => {
                // Writer task is gone; drop the signal silently. This
                // path is only hit during shutdown.
                return;
            }
        }
    }
}

/// Unix milliseconds. Pulled out for tests.
fn now_ms() -> i64 {
    let nanos = time::OffsetDateTime::now_utc().unix_timestamp_nanos();
    (nanos / 1_000_000) as i64
}

#[cfg(test)]
mod tests {
    use super::*;
    use corlinman_evolution::EvolutionStore;
    use corlinman_hooks::{HookBus, HookEvent};
    use std::collections::BTreeMap;
    use std::time::Duration;
    use tempfile::TempDir;

    async fn fresh_repo() -> (TempDir, SignalsRepo, EvolutionStore) {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("evolution.sqlite");
        let store = EvolutionStore::open(&path).await.unwrap();
        let repo = SignalsRepo::new(store.pool().clone());
        (tmp, repo, store)
    }

    /// Wait up to `total` for `cond` to return `true`, polling every 25ms.
    /// Returns the final boolean so callers can `assert!` on it without
    /// trying to figure out a global sleep budget.
    async fn await_until<F, Fut>(total: Duration, mut cond: F) -> bool
    where
        F: FnMut() -> Fut,
        Fut: std::future::Future<Output = bool>,
    {
        let deadline = tokio::time::Instant::now() + total;
        loop {
            if cond().await {
                return true;
            }
            if tokio::time::Instant::now() >= deadline {
                return false;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    }

    fn tool_failed(tool: &str, code: Option<&str>) -> HookEvent {
        HookEvent::ToolCalled {
            tool: tool.into(),
            runner_id: "test-runner".into(),
            duration_ms: 12,
            ok: false,
            error_code: code.map(str::to_string),
            tenant_id: None,
        }
    }

    #[test]
    fn adapt_classifies_tool_called_variants() {
        // ok = true → not adapted.
        assert!(adapt(&HookEvent::ToolCalled {
            tool: "web_search".into(),
            runner_id: "r1".into(),
            duration_ms: 5,
            ok: true,
            error_code: None,
            tenant_id: None,
        })
        .is_none());

        // failed (non-timeout) → severity Error, kind tool.call.failed.
        let s = adapt(&tool_failed("web_search", Some("disconnected"))).unwrap();
        assert_eq!(s.event_kind, "tool.call.failed");
        assert_eq!(s.severity, SignalSeverity::Error);
        assert_eq!(s.target.as_deref(), Some("web_search"));
        // Phase 4 W1.5 (next-tasks A1): legacy default attribution.
        assert_eq!(s.tenant_id, "default");

        // timeout → severity Warn, kind tool.call.timeout.
        let s = adapt(&tool_failed("web_search", Some("timeout"))).unwrap();
        assert_eq!(s.event_kind, "tool.call.timeout");
        assert_eq!(s.severity, SignalSeverity::Warn);
    }

    /// Phase 4 W1.5 (next-tasks A1): when the source HookEvent
    /// carries a tenant_id, the resulting signal must wear it
    /// instead of falling back to "default". Pins the propagation
    /// the chat-lifecycle follow-up will rely on.
    #[test]
    fn adapt_propagates_tenant_id_from_tool_called() {
        let s = adapt(&HookEvent::ToolCalled {
            tool: "web_search".into(),
            runner_id: "r1".into(),
            duration_ms: 12,
            ok: false,
            error_code: Some("timeout".into()),
            tenant_id: Some("acme".into()),
        })
        .unwrap();
        assert_eq!(s.tenant_id, "acme");
    }

    #[test]
    fn adapt_classifies_approval_decisions() {
        // allow → ignored.
        assert!(adapt(&HookEvent::ApprovalDecided {
            id: "a1".into(),
            decision: "allow".into(),
            decider: Some("op".into()),
            decided_at_ms: 0,
            tenant_id: None,
        })
        .is_none());

        // deny → approval.rejected, severity Warn.
        let s = adapt(&HookEvent::ApprovalDecided {
            id: "a2".into(),
            decision: "deny".into(),
            decider: Some("op".into()),
            decided_at_ms: 0,
            tenant_id: None,
        })
        .unwrap();
        assert_eq!(s.event_kind, "approval.rejected");
        assert_eq!(s.severity, SignalSeverity::Warn);
        assert_eq!(s.target.as_deref(), Some("a2"));
        assert_eq!(s.tenant_id, "default");

        // timeout → also approval.rejected (non-allow).
        let s = adapt(&HookEvent::ApprovalDecided {
            id: "a3".into(),
            decision: "timeout".into(),
            decider: None,
            decided_at_ms: 0,
            tenant_id: None,
        })
        .unwrap();
        assert_eq!(s.event_kind, "approval.rejected");
    }

    #[test]
    fn adapt_propagates_tenant_id_from_approval_decided() {
        let s = adapt(&HookEvent::ApprovalDecided {
            id: "a4".into(),
            decision: "deny".into(),
            decider: Some("op".into()),
            decided_at_ms: 0,
            tenant_id: Some("bravo".into()),
        })
        .unwrap();
        assert_eq!(s.tenant_id, "bravo");
    }

    #[test]
    fn adapt_skips_unrelated_events() {
        let mut tags = BTreeMap::new();
        tags.insert("k".into(), "v".into());
        let untracked = vec![
            HookEvent::GatewayStartup {
                version: "0.1.0".into(),
            },
            HookEvent::MessageReceived {
                channel: "telegram".into(),
                session_key: "s".into(),
                content: "hi".into(),
                metadata: serde_json::Value::Null,
            },
            HookEvent::Telemetry {
                node_id: "n".into(),
                metric: "m".into(),
                value: 1.0,
                tags,
            },
        ];
        for e in untracked {
            assert!(adapt(&e).is_none(), "unexpected adapt for {:?}", e.kind());
        }
    }

    #[tokio::test]
    async fn observer_persists_emitted_events() {
        let (_tmp, repo, _store) = fresh_repo().await;
        let bus = Arc::new(HookBus::new(64));
        let cfg = EvolutionObserverConfig {
            enabled: true,
            db_path: "unused".into(),
            queue_capacity: 32,
        };

        let _writer = spawn(bus.clone(), repo.clone(), &cfg);

        // 5 distinct tool failures.
        for i in 0..5 {
            bus.emit(tool_failed(&format!("tool_{i}"), Some("disconnected")))
                .await
                .unwrap();
        }

        let ok = await_until(Duration::from_secs(1), || {
            let repo = repo.clone();
            async move {
                let rows = repo.list_since(0, Some("tool.call.failed"), 100).await;
                rows.map(|r| r.len() >= 5).unwrap_or(false)
            }
        })
        .await;
        assert!(
            ok,
            "expected 5 tool.call.failed signals to be persisted within 1s"
        );
    }

    #[tokio::test]
    async fn observer_drops_oldest_on_overflow() {
        // Tiny queue + writer that holds the receiver lock by virtue of
        // being parked on `recv` — the eviction path takes that same lock,
        // serialising overflows. We push a burst far larger than the
        // capacity and assert that the dropped counter advanced.
        let (_tmp, repo, _store) = fresh_repo().await;
        let bus = Arc::new(HookBus::new(16384));
        let cfg = EvolutionObserverConfig {
            enabled: true,
            db_path: "unused".into(),
            queue_capacity: 8,
        };
        // Snapshot the global counter so this test composes with others
        // that share the same registry.
        let before_dropped = EVOLUTION_SIGNALS_DROPPED.get();

        let _writer = spawn(bus.clone(), repo.clone(), &cfg);

        // Burst 200 events synchronously; the observer's mpsc capacity of
        // 8 + the writer's drain rate guarantees at least some overflow.
        for i in 0..200 {
            bus.emit(tool_failed(&format!("tool_{i}"), Some("disconnected")))
                .await
                .unwrap();
        }

        let ok = await_until(Duration::from_secs(1), || async {
            EVOLUTION_SIGNALS_DROPPED.get() > before_dropped
        })
        .await;
        assert!(
            ok,
            "expected dropped counter to advance under burst (before={before_dropped}, \
             after={})",
            EVOLUTION_SIGNALS_DROPPED.get()
        );
    }
}
