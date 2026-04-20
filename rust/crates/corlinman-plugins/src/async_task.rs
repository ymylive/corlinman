//! Async task registry: pair `taskId` → `oneshot::Sender<Bytes>` for plugin-callback.
//
// TODO: `Registry { map: DashMap<String, oneshot::Sender<Bytes>> }`; `park(task_id, tx)`
//       + `resolve(task_id, payload)` consumed by `routes::plugin_callback`.
// TODO: enforce TTL via `backoff::DEFAULT_SCHEDULE`; on expiry, send `Err(Timeout)` downstream.
