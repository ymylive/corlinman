//! Cron expression parsing + tick loop.
//
// TODO: adopt `tokio-cron-scheduler` or a minimal in-house cron parser (5-field).
// TODO: expose `Scheduler::spawn(jobs: Vec<Job>, cancel) -> JoinHandle`; each job
//       opens a new tokio task with its own request_id for tracing.
