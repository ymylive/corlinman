//! `corlinman qa {run,replay,bench}` — scenario runner (plan §8 C1).
//
// TODO: `run` loads `qa/scenarios/*.yaml`, boots a gateway, replays requests,
//       asserts per-scenario expectations; emits a parity report.
// TODO: `replay --from <pcap>` replays captured SSE traffic; `bench` prints histograms.

use clap::Parser;

#[derive(Debug, Parser)]
pub struct Args {
    /// Filter scenarios by glob (e.g. `chat-*`).
    #[arg(long)]
    pub filter: Option<String>,

    /// Emit JUnit XML to this path for CI ingestion.
    #[arg(long)]
    pub junit: Option<std::path::PathBuf>,
}

pub async fn run(_args: Args) -> anyhow::Result<()> {
    panic!("TODO: corlinman qa — YAML scenario runner");
}
