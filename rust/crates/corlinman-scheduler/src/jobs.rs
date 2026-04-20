//! Job trait + built-in job kinds (`run_agent`, `run_plugin`, `run_shell`).
//
// TODO: `#[async_trait] trait Job { async fn run(ctx: &JobContext) -> Result<(), CorlinmanError> }`.
// TODO: load jobs from `config.scheduler_jobs` (YAML list); each entry includes
//       cron expression + kind + kind-specific payload.
