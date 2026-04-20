//! `corlinman config {show,get,set,validate,init,diff}` — typed edits to
//! `config.toml`.
//!
//! Every subcommand resolves the config path via `--path` or, failing that,
//! [`Config::default_path`] (honours `CORLINMAN_DATA_DIR`). `show` / `get` run
//! through [`Config::redacted`] so secrets never leak.

use std::path::PathBuf;

use anyhow::{anyhow, Context, Result};
use clap::Subcommand;
use corlinman_core::config::{self, Config, IssueLevel};

#[derive(Debug, Subcommand)]
pub enum Cmd {
    /// Print the full config (secrets redacted).
    Show {
        /// Emit JSON instead of TOML.
        #[arg(long)]
        json: bool,
        /// Explicit config path; defaults to `$CORLINMAN_DATA_DIR/config.toml`.
        #[arg(long)]
        path: Option<PathBuf>,
    },
    /// Read a dotted key (e.g. `server.port`).
    Get {
        key: String,
        #[arg(long)]
        path: Option<PathBuf>,
    },
    /// Set a dotted scalar key and save.
    Set {
        key: String,
        value: String,
        #[arg(long)]
        path: Option<PathBuf>,
    },
    /// Run every validator; non-zero exit on any issue.
    Validate {
        #[arg(long)]
        path: Option<PathBuf>,
    },
    /// Write a default config to `~/.corlinman/config.toml` (or `--path`).
    Init {
        #[arg(long)]
        path: Option<PathBuf>,
        /// Overwrite an existing file.
        #[arg(long)]
        force: bool,
    },
    /// Diff current config against defaults (M7 full implementation; stub for now).
    Diff {
        #[arg(long)]
        path: Option<PathBuf>,
    },
}

pub async fn run(cmd: Cmd) -> Result<()> {
    match cmd {
        Cmd::Show { json, path } => show(path, json),
        Cmd::Get { key, path } => get(path, &key),
        Cmd::Set { key, value, path } => set(path, &key, &value),
        Cmd::Validate { path } => validate(path),
        Cmd::Init { path, force } => init(path, force),
        Cmd::Diff { path } => diff(path),
    }
}

fn resolve_path(explicit: Option<PathBuf>) -> PathBuf {
    explicit.unwrap_or_else(Config::default_path)
}

fn load(path: &std::path::Path) -> Result<Config> {
    Config::load_from_path(path).with_context(|| format!("load config from {}", path.display()))
}

fn show(path: Option<PathBuf>, json: bool) -> Result<()> {
    let p = resolve_path(path);
    let cfg = load(&p)?.redacted();
    if json {
        println!("{}", serde_json::to_string_pretty(&cfg)?);
    } else {
        println!("{}", toml::to_string_pretty(&cfg)?);
    }
    Ok(())
}

fn get(path: Option<PathBuf>, key: &str) -> Result<()> {
    let p = resolve_path(path);
    let cfg = load(&p)?.redacted();
    let value = config::get_dotted(&cfg, key).map_err(|e| anyhow!("cannot read '{key}': {e}"))?;
    println!("{value}");
    Ok(())
}

fn set(path: Option<PathBuf>, key: &str, value: &str) -> Result<()> {
    let p = resolve_path(path);
    let current = load(&p)?;
    let updated = config::set_dotted(&current, key, value)
        .map_err(|e| anyhow!("cannot set '{key} = {value}': {e}"))?;
    let issues = updated.validate_report();
    if !issues.is_empty() {
        eprintln!(
            "warning: config still has {} issue(s) after this set:",
            issues.len()
        );
        for i in &issues {
            eprintln!("  [{}] {}: {}", i.code, i.path, i.message);
        }
    }
    updated.save_to_path(&p)?;
    println!("updated {} -> {}", key, value);
    Ok(())
}

fn validate(path: Option<PathBuf>) -> Result<()> {
    let p = resolve_path(path);
    let cfg = load(&p)?;
    let issues = cfg.validate_report();
    if issues.is_empty() {
        println!("{}: OK ({} issues)", p.display(), 0);
        return Ok(());
    }

    // Partition into errors vs warnings: only errors flip the exit code. A
    // freshly `config init`-ed default config produces warn-level issues (e.g.
    // `no_provider_enabled`) and should still pass validation.
    let errors = issues
        .iter()
        .filter(|i| i.level == IssueLevel::Error)
        .count();
    let warnings = issues.len() - errors;

    // Warnings go to stdout, errors to stderr; both lines carry the level so
    // downstream tooling can grep.
    for i in &issues {
        let (stream_err, tag) = match i.level {
            IssueLevel::Error => (true, "error"),
            IssueLevel::Warn => (false, "warn"),
        };
        let line = format!("  [{tag}] [{}] {}: {}", i.code, i.path, i.message);
        if stream_err {
            eprintln!("{line}");
        } else {
            println!("{line}");
        }
    }

    if errors > 0 {
        eprintln!(
            "{}: {} error(s), {} warning(s)",
            p.display(),
            errors,
            warnings
        );
        std::process::exit(1);
    }
    println!("{}: OK ({} warning(s), 0 errors)", p.display(), warnings);
    Ok(())
}

fn init(path: Option<PathBuf>, force: bool) -> Result<()> {
    let p = resolve_path(path);
    if p.exists() && !force {
        return Err(anyhow!(
            "{} already exists; pass --force to overwrite",
            p.display()
        ));
    }
    let cfg = Config::default();
    cfg.save_to_path(&p)?;
    println!("wrote default config to {}", p.display());
    Ok(())
}

fn diff(path: Option<PathBuf>) -> Result<()> {
    // TODO(M7): implement a proper structural diff (toml::Value walk with
    // coloured output). For the beachhead we just enumerate the field groups
    // that differ from Config::default() as a best-effort sketch.
    let p = resolve_path(path);
    let current = load(&p)?;
    let default = Config::default();

    let cur_toml = toml::to_string_pretty(&current.redacted())?;
    let def_toml = toml::to_string_pretty(&default)?;
    if cur_toml == def_toml {
        println!("no differences from defaults");
        return Ok(());
    }
    println!("# current (redacted):");
    println!("{cur_toml}");
    println!("# defaults:");
    println!("{def_toml}");
    Ok(())
}
