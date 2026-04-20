//! Async task registry: pair `task_id` → `oneshot::Sender<Value>` for
//! `/plugin-callback/:task_id`.
//!
//! Async plugins may return `{"result": {"task_id": "tsk_..."}}`, which the
//! stdio runtime surfaces as [`crate::runtime::PluginOutput::AcceptedForLater`].
//! The gateway parks the tool_call until a matching HTTP callback arrives (or
//! a deadline elapses); this module owns the park/complete map.
//!
//! Concurrency model: `DashMap<String, Entry>` (lock-free per-shard). Each
//! `Entry` carries the `oneshot::Sender<Value>` and the `Instant` the task was
//! registered so the background sweeper can expire entries that never get a
//! callback.
//!
//! The `Value` payload is whatever JSON the plugin posts to `/plugin-callback/
//! :task_id`. Gateway callers are responsible for shaping that into a
//! `ToolResult` envelope; the registry itself is transport-agnostic.

use std::sync::Arc;
use std::time::{Duration, Instant};

use dashmap::DashMap;
use thiserror::Error;
use tokio::sync::oneshot;

/// A parked task: the `Sender` half used to wake the waiter, plus the
/// `Instant` it was registered so the sweep loop can expire stale entries.
#[derive(Debug)]
struct Entry {
    tx: oneshot::Sender<serde_json::Value>,
    registered_at: Instant,
}

/// Error outcomes for [`AsyncTaskRegistry::complete`].
#[derive(Debug, Error, PartialEq, Eq)]
pub enum CompleteError {
    /// No pending entry for the given `task_id` — either it was never
    /// registered or it already completed / expired.
    #[error("task_not_found")]
    NotFound,
    /// Callback arrived but the waiter had already been dropped (client
    /// disconnected, timeout fired). The registry removes the stale entry
    /// either way; this tells the HTTP handler to return 410/409.
    #[error("waiter_dropped")]
    WaiterDropped,
}

/// Registry for async plugin tasks awaiting an HTTP callback.
///
/// Cheap to share behind `Arc`; every method is `&self` and internally
/// locks only the relevant `DashMap` shard.
#[derive(Debug, Default)]
pub struct AsyncTaskRegistry {
    pending: DashMap<String, Entry>,
}

impl AsyncTaskRegistry {
    pub fn new() -> Self {
        Self {
            pending: DashMap::new(),
        }
    }

    /// Register a pending async task and obtain a receiver that resolves
    /// when [`Self::complete`] is called for the same `task_id`.
    ///
    /// If `task_id` is already registered the previous entry is evicted
    /// (its sender drops, waking the old waiter with `RecvError`). This is
    /// a defensive path for plugins that reuse task ids; in practice ids
    /// should be unique per call.
    pub fn register(&self, task_id: String) -> oneshot::Receiver<serde_json::Value> {
        let (tx, rx) = oneshot::channel();
        self.pending.insert(
            task_id,
            Entry {
                tx,
                registered_at: Instant::now(),
            },
        );
        rx
    }

    /// Complete a pending task. Returns `NotFound` when no entry exists,
    /// `WaiterDropped` when the entry was removed but the receiver had
    /// already been dropped (so the sender errors out).
    pub fn complete(&self, task_id: &str, payload: serde_json::Value) -> Result<(), CompleteError> {
        let (_key, entry) = self
            .pending
            .remove(task_id)
            .ok_or(CompleteError::NotFound)?;
        entry
            .tx
            .send(payload)
            .map_err(|_| CompleteError::WaiterDropped)
    }

    /// Remove a pending task without delivering a payload. Used by the
    /// gateway when its wait times out or the client disconnects, so a
    /// late callback gets `NotFound` instead of racing into a closed
    /// channel.
    pub fn cancel(&self, task_id: &str) -> bool {
        self.pending.remove(task_id).is_some()
    }

    /// Whether a given task id is still pending. Intended for tests / admin.
    pub fn is_pending(&self, task_id: &str) -> bool {
        self.pending.contains_key(task_id)
    }

    /// Number of pending entries. Exposed for metrics / tests.
    pub fn len(&self) -> usize {
        self.pending.len()
    }

    pub fn is_empty(&self) -> bool {
        self.pending.is_empty()
    }

    /// Drop pending entries older than `ttl`. Returns the number evicted.
    /// The evicted senders are dropped, which surfaces as `RecvError` on
    /// any blocked waiter — callers interpret that as a timeout.
    pub fn sweep_expired(&self, ttl: Duration) -> usize {
        let now = Instant::now();
        let expired: Vec<String> = self
            .pending
            .iter()
            .filter(|e| now.duration_since(e.value().registered_at) > ttl)
            .map(|e| e.key().clone())
            .collect();
        let mut count = 0;
        for key in expired {
            if self.pending.remove(&key).is_some() {
                count += 1;
            }
        }
        count
    }

    /// Spawn a background task that periodically calls [`Self::sweep_expired`].
    ///
    /// Returns the join handle so callers can abort it at shutdown.
    pub fn start_sweep(
        self: Arc<Self>,
        interval: Duration,
        ttl: Duration,
    ) -> tokio::task::JoinHandle<()> {
        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(interval);
            // Skip the first immediate tick so tests without time control
            // don't see a sweep before registering anything.
            ticker.tick().await;
            loop {
                ticker.tick().await;
                let evicted = self.sweep_expired(ttl);
                if evicted > 0 {
                    tracing::debug!(
                        evicted,
                        pending = self.len(),
                        "async_task.sweep.evicted_expired"
                    );
                }
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[tokio::test]
    async fn register_then_complete_delivers_payload() {
        let reg = AsyncTaskRegistry::new();
        let rx = reg.register("tsk_1".into());
        assert!(reg.is_pending("tsk_1"));

        reg.complete("tsk_1", json!({"result": "hello"})).unwrap();
        let value = rx.await.expect("waiter receives payload");
        assert_eq!(value["result"], "hello");
        assert!(!reg.is_pending("tsk_1"));
    }

    #[tokio::test]
    async fn complete_unknown_task_returns_not_found() {
        let reg = AsyncTaskRegistry::new();
        let err = reg.complete("nope", json!({})).unwrap_err();
        assert_eq!(err, CompleteError::NotFound);
    }

    #[tokio::test]
    async fn complete_twice_second_call_is_not_found() {
        let reg = AsyncTaskRegistry::new();
        let rx = reg.register("tsk_2".into());
        reg.complete("tsk_2", json!({"n": 1})).unwrap();
        let _ = rx.await;
        let err = reg.complete("tsk_2", json!({"n": 2})).unwrap_err();
        assert_eq!(err, CompleteError::NotFound);
    }

    #[tokio::test]
    async fn dropped_waiter_surfaces_as_waiter_dropped() {
        let reg = AsyncTaskRegistry::new();
        let rx = reg.register("tsk_3".into());
        drop(rx);
        let err = reg.complete("tsk_3", json!({})).unwrap_err();
        assert_eq!(err, CompleteError::WaiterDropped);
    }

    #[tokio::test]
    async fn sweep_expired_removes_old_entries() {
        let reg = AsyncTaskRegistry::new();
        let _rx_old = reg.register("old".into());
        tokio::time::sleep(Duration::from_millis(20)).await;
        let _rx_new = reg.register("new".into());
        // TTL shorter than the gap between the two registers.
        let evicted = reg.sweep_expired(Duration::from_millis(10));
        assert_eq!(evicted, 1);
        assert!(!reg.is_pending("old"));
        assert!(reg.is_pending("new"));
    }
}
