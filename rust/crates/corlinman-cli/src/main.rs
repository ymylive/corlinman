//! corlinman CLI entry point (clap derive).
//!
//! Subcommands (see plan §2 `corlinman-cli` + §8 borrowed patterns):
//!   onboard / doctor / plugins / config / dev / qa
//!
//! Each subcommand lives in `cmd/<name>.rs`; `main` only dispatches.

use clap::{Parser, Subcommand};

mod cmd;

/// corlinman — self-hosted LLM toolbox with Rust gateway and Python AI plane.
#[derive(Debug, Parser)]
#[command(name = "corlinman", version, about)]
struct Cli {
    #[command(subcommand)]
    command: Cmd,
}

#[derive(Debug, Subcommand)]
enum Cmd {
    /// Interactive onboarding wizard (non-interactive via `--accept-risk`).
    Onboard(cmd::onboard::Args),
    /// Run diagnostic checks across config / upstream / manifests.
    Doctor(cmd::doctor::Args),
    /// Plugin introspection: list / inspect / doctor.
    #[command(subcommand)]
    Plugins(cmd::plugins::Cmd),
    /// Configuration management (show / set / validate).
    #[command(subcommand)]
    Config(cmd::config::Cmd),
    /// Developer helpers (watch / format / typecheck).
    #[command(subcommand)]
    Dev(cmd::dev::Cmd),
    /// Run QA scenarios from `qa/scenarios/*.yaml`, or execute perf bench.
    #[command(subcommand)]
    Qa(cmd::qa::Cmd),
    /// Vector index: stats / query / rebuild (Sprint 3 T5).
    #[command(subcommand)]
    Vector(cmd::vector::Cmd),
    /// Multi-tenant admin: create tenants and list the roster
    /// (Phase 4 W1 4-1A Item 4).
    #[command(subcommand)]
    Tenant(cmd::tenant::Cmd),
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Cmd::Onboard(args) => cmd::onboard::run(args).await,
        Cmd::Doctor(args) => cmd::doctor::run(args).await,
        Cmd::Plugins(sub) => cmd::plugins::run(sub).await,
        Cmd::Config(sub) => cmd::config::run(sub).await,
        Cmd::Dev(sub) => cmd::dev::run(sub).await,
        Cmd::Qa(sub) => cmd::qa::run(cmd::qa::Args { cmd: sub }).await,
        Cmd::Vector(sub) => cmd::vector::run(sub).await,
        Cmd::Tenant(sub) => cmd::tenant::run(sub).await,
    }
}
