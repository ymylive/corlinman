//! `corlinman dev {watch,gen-proto,check}` — developer loop helpers.
//
// TODO: `watch` spawns `cargo watch -x run -p corlinman-gateway` + `uv run python -m corlinman_server`.
// TODO: `gen-proto` shells out to `scripts/gen-proto.sh`; `check` runs fmt + clippy + pytest.

use clap::Subcommand;

#[derive(Debug, Subcommand)]
pub enum Cmd {
    Watch,
    GenProto,
    Check,
}

pub async fn run(_cmd: Cmd) -> anyhow::Result<()> {
    panic!("TODO: corlinman dev — thin wrapper around scripts/");
}
