//! corlinman-scheduler — cron-based periodic job runner.
//!
//! Jobs run with a shared `JobContext` that exposes `agent_client`,
//! `plugin_registry`, and `vector_store` handles (plan §5.4). Shutdown of the
//! gateway cascades into the scheduler via a shared `CancellationToken`.

pub mod cron;
pub mod jobs;
