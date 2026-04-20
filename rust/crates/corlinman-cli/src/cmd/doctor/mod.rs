//! `corlinman doctor` — run diagnostic checks (plan §8 B3).
//!
//! Contract:
//!   * Instantiate every `DoctorCheck` in [`checks::all`], run sequentially.
//!   * Human mode: one line per check with ✓ / ! / ✗, then a summary row.
//!   * `--json`: array of `{name, status, message, hint?}` on stdout.
//!   * `--module <name>`: run only the matching check (case-sensitive).
//!   * Exit code: non-zero iff any check returned `Fail`. Warnings are
//!     informational — we don't want `doctor` in a CI loop to fail just
//!     because the user hasn't configured a provider yet.

use clap::Parser;

pub mod checks;

use checks::{all as all_checks, CheckReport, DoctorContext};

#[derive(Debug, Parser)]
pub struct Args {
    /// Emit JSON instead of human-readable output.
    #[arg(long)]
    pub json: bool,

    /// Run a single check by name (e.g. `config`, `upstream`, `manifest`).
    #[arg(long)]
    pub module: Option<String>,
}

pub async fn run(args: Args) -> anyhow::Result<()> {
    let ctx = build_context();

    let mut checks = all_checks();
    if let Some(filter) = args.module.as_deref() {
        checks.retain(|c| c.name() == filter);
        if checks.is_empty() {
            anyhow::bail!("no check named '{filter}'");
        }
    }

    let mut reports: Vec<CheckReport> = Vec::with_capacity(checks.len());
    for check in &checks {
        let name = check.name().to_string();
        let result = check.run(&ctx).await;
        reports.push(CheckReport::new(&name, &result));
    }

    if args.json {
        let out = serde_json::to_string_pretty(&reports)?;
        println!("{out}");
    } else {
        print_human(&reports);
    }

    let has_fail = reports.iter().any(|r| r.status == "fail");
    if has_fail {
        std::process::exit(1);
    }
    Ok(())
}

fn build_context() -> DoctorContext {
    use corlinman_core::config::Config;
    let config_path = Config::default_path();
    let data_dir = config_path
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| std::path::PathBuf::from("."));
    let config = if config_path.exists() {
        Config::load_from_path(&config_path).ok()
    } else {
        None
    };
    DoctorContext {
        data_dir,
        config,
        config_path,
    }
}

fn print_human(reports: &[CheckReport]) {
    let name_width = reports
        .iter()
        .map(|r| r.name.len())
        .max()
        .unwrap_or(0)
        .max(8);
    let mut fails = 0;
    let mut warns = 0;
    let mut oks = 0;
    for r in reports {
        let glyph = match r.status.as_str() {
            "ok" => {
                oks += 1;
                "✓"
            }
            "warn" => {
                warns += 1;
                "!"
            }
            _ => {
                fails += 1;
                "✗"
            }
        };
        println!(
            "{glyph} {:<width$}  {}",
            r.name,
            r.message,
            width = name_width
        );
        if let Some(hint) = &r.hint {
            println!("  {:<width$}  hint: {hint}", "", width = name_width);
        }
    }
    println!();
    println!("{fails} fail, {warns} warn, {oks} ok");
}
