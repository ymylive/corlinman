//! `corlinman replay` — Phase 4 W2 4-2D trajectory replay.
//!
//! Reconstructs a stored session by key from `sessions.sqlite` and
//! emits a deterministic transcript. Two output formats:
//!
//! - `--output human` (default): chat-style rendering with role
//!   labels + timestamps. Suited for a terminal review.
//! - `--output json`: pretty-printed JSON matching the
//!   `/admin/sessions/:key/replay` HTTP route's wire shape, so
//!   `corlinman replay X --output json | jq ...` is a valid
//!   debugging workflow.
//!
//! Tenant scoping mirrors the gateway: pass `--tenant-id <slug>`
//! (defaults to `default`) and the CLI reads from
//! `<data_dir>/tenants/<tenant>/sessions.sqlite`.

use std::path::{Path, PathBuf};

use anyhow::{anyhow, bail, Context, Result};
use clap::{Parser, ValueEnum};
use corlinman_replay::{replay, ReplayError, ReplayMode, ReplayOutput};
use corlinman_tenant::TenantId;

#[derive(Debug, Parser)]
pub struct Args {
    /// Session key to replay (e.g. `qq:1234`,
    /// `telegram:private:9001`).
    pub session_id: String,
    /// Override the data directory. Defaults to
    /// `$CORLINMAN_DATA_DIR` or `~/.corlinman`.
    #[arg(long)]
    pub data_dir: Option<PathBuf>,
    /// Replay mode. `transcript` (default) is read-only
    /// deterministic dump. `rerun` returns the wire shape with a
    /// `not_implemented_yet` marker; the diff renderer ships in
    /// Wave 2.5.
    #[arg(long, value_enum, default_value_t = ModeArg::Transcript)]
    pub mode: ModeArg,
    /// Output format. `human` formats the transcript for terminal
    /// review; `json` emits the same shape the HTTP route does.
    #[arg(long, value_enum, default_value_t = OutputArg::Human)]
    pub output: OutputArg,
    /// Tenant slug — defaults to the reserved `default`. Validated
    /// via `TenantId::new` so an invalid slug is caught before any
    /// filesystem read.
    #[arg(long, default_value = "default")]
    pub tenant_id: String,
}

#[derive(Debug, Clone, Copy, ValueEnum)]
pub enum ModeArg {
    Transcript,
    Rerun,
}

impl From<ModeArg> for ReplayMode {
    fn from(value: ModeArg) -> Self {
        match value {
            ModeArg::Transcript => Self::Transcript,
            ModeArg::Rerun => Self::Rerun,
        }
    }
}

#[derive(Debug, Clone, Copy, ValueEnum)]
pub enum OutputArg {
    Human,
    Json,
}

pub async fn run(args: Args) -> Result<()> {
    let tenant = TenantId::new(args.tenant_id.clone())
        .with_context(|| format!("invalid --tenant-id {:?}", args.tenant_id))?;

    let data_dir = resolve_data_dir(args.data_dir.as_deref());
    let mode: ReplayMode = args.mode.into();

    let output = match replay(&data_dir, &tenant, &args.session_id, mode).await {
        Ok(out) => out,
        Err(ReplayError::SessionNotFound(key)) => {
            bail!(
                "session not found: {key:?} under tenant {:?} (data dir {})",
                tenant.as_str(),
                data_dir.display()
            );
        }
        Err(other) => return Err(anyhow!(other).context("replay failed")),
    };

    match args.output {
        OutputArg::Human => print_human(&output),
        OutputArg::Json => print_json(&output)?,
    }

    Ok(())
}

fn print_human(out: &ReplayOutput) {
    println!(
        "session: {} · tenant: {} · mode: {} · {} message(s)",
        out.session_key, out.summary.tenant_id, out.mode, out.summary.message_count,
    );
    if let Some(marker) = &out.summary.rerun_diff {
        println!("rerun: {marker} (Wave 2.5 deferred)");
    }
    println!();

    for (i, msg) in out.transcript.iter().enumerate() {
        let role_label = match msg.role.as_str() {
            "user" => "USER",
            "assistant" => "ASSISTANT",
            "system" => "SYSTEM",
            "tool" => "TOOL",
            other => other,
        };
        println!("[{:>3}] {} · {}", i + 1, role_label, msg.ts);
        for line in msg.content.lines() {
            println!("    {line}");
        }
        println!();
    }
}

fn print_json(out: &ReplayOutput) -> Result<()> {
    let json = serde_json::to_string_pretty(out).context("serialize replay output")?;
    println!("{json}");
    Ok(())
}

fn resolve_data_dir(override_path: Option<&Path>) -> PathBuf {
    if let Some(p) = override_path {
        return p.to_path_buf();
    }
    if let Ok(env) = std::env::var("CORLINMAN_DATA_DIR") {
        return PathBuf::from(env);
    }
    dirs::home_dir().unwrap_or_default().join(".corlinman")
}

#[cfg(test)]
mod tests {
    use super::*;
    use corlinman_core::{SessionMessage, SessionStore, SqliteSessionStore};
    use corlinman_replay::sessions_db_path;
    use tempfile::TempDir;

    #[tokio::test]
    async fn run_human_output_renders_transcript() {
        let tmp = TempDir::new().unwrap();
        let tenant = TenantId::legacy_default();
        let path = sessions_db_path(tmp.path(), &tenant);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let store = SqliteSessionStore::open(&path).await.unwrap();
        store
            .append("test", SessionMessage::user("hi"))
            .await
            .unwrap();

        run(Args {
            session_id: "test".into(),
            data_dir: Some(tmp.path().to_path_buf()),
            mode: ModeArg::Transcript,
            output: OutputArg::Human,
            tenant_id: "default".into(),
        })
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn run_rejects_invalid_tenant_slug() {
        let tmp = TempDir::new().unwrap();
        let err = run(Args {
            session_id: "x".into(),
            data_dir: Some(tmp.path().to_path_buf()),
            mode: ModeArg::Transcript,
            output: OutputArg::Json,
            tenant_id: "BAD!!".into(),
        })
        .await
        .expect_err("invalid slug must fail");
        assert!(err.to_string().contains("invalid --tenant-id"));
    }

    #[tokio::test]
    async fn run_reports_missing_session_clearly() {
        let tmp = TempDir::new().unwrap();
        // Pre-create the per-tenant sessions.sqlite so the open path
        // succeeds; the replay primitive then returns SessionNotFound
        // which our wrapper converts to a clear error string.
        let tenant = TenantId::legacy_default();
        let path = sessions_db_path(tmp.path(), &tenant);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let _store = SqliteSessionStore::open(&path).await.unwrap();

        let err = run(Args {
            session_id: "ghost".into(),
            data_dir: Some(tmp.path().to_path_buf()),
            mode: ModeArg::Transcript,
            output: OutputArg::Human,
            tenant_id: "default".into(),
        })
        .await
        .expect_err("missing session must fail");
        assert!(err.to_string().contains("session not found"));
    }
}
