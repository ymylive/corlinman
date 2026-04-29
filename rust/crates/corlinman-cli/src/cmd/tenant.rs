//! `corlinman tenant` — Phase 4 W1 4-1A Item 4.
//!
//! Two subcommands:
//!
//! - `corlinman tenant create <slug>` — register a new tenant in
//!   `<data_dir>/tenants.sqlite`, create the per-tenant directory
//!   tree under `<data_dir>/tenants/<slug>/`, and seed an admin
//!   credential row in `tenant_admins` so the operator can sign in
//!   to the new tenant from day one.
//! - `corlinman tenant list` — print the current tenant roster as a
//!   table.
//!
//! Slug validation is delegated to [`corlinman_tenant::TenantId`];
//! the regex `^[a-z][a-z0-9-]{0,62}$` is enforced at parse time so
//! invalid slugs never reach the SQLite write. Password hashing uses
//! `argon2id` with the `argon2` workspace dependency, matching the
//! gateway's existing admin credential format (`$argon2id$v=19$...`).
//!
//! Data directory resolution mirrors `corlinman-gateway::server`:
//! either the `CORLINMAN_DATA_DIR` env var, or the `[server].data_dir`
//! field in the loaded config, or the default `~/.corlinman`. The CLI
//! does *not* require a running gateway — it operates directly on the
//! `tenants.sqlite` file the gateway will read at boot.

use std::io::Write;
use std::path::{Path, PathBuf};

use anyhow::{anyhow, bail, Context, Result};
use argon2::password_hash::{rand_core::OsRng, PasswordHasher, SaltString};
use argon2::Argon2;
use clap::{Args, Subcommand};
use corlinman_tenant::{tenant_root_dir, AdminDb, AdminDbError, TenantId};
use tabled::{settings::Style, Table, Tabled};

#[derive(Debug, Subcommand)]
pub enum Cmd {
    /// Create a new tenant: register in `tenants.sqlite`, create the
    /// per-tenant data dir, and seed an admin credential.
    Create(CreateArgs),
    /// List the current tenant roster.
    List(ListArgs),
}

#[derive(Debug, Args)]
pub struct CreateArgs {
    /// Tenant slug (`[a-z][a-z0-9-]{0,62}`). Must be unique across
    /// the deployment.
    pub slug: String,
    /// Human-readable display name shown in the admin UI. Defaults
    /// to the slug when omitted.
    #[arg(long)]
    pub display_name: Option<String>,
    /// Override the data directory. Defaults to
    /// `$CORLINMAN_DATA_DIR` or `~/.corlinman`.
    #[arg(long)]
    pub data_dir: Option<PathBuf>,
    /// Initial admin username for this tenant. Required.
    #[arg(long)]
    pub admin_username: String,
    /// Plaintext admin password. When omitted the CLI prompts on
    /// stdin (echo disabled) — recommended path so credentials don't
    /// land in shell history.
    #[arg(long)]
    pub admin_password: Option<String>,
}

#[derive(Debug, Args)]
pub struct ListArgs {
    /// Override the data directory. Defaults to
    /// `$CORLINMAN_DATA_DIR` or `~/.corlinman`.
    #[arg(long)]
    pub data_dir: Option<PathBuf>,
}

pub async fn run(cmd: Cmd) -> Result<()> {
    match cmd {
        Cmd::Create(args) => run_create(args).await,
        Cmd::List(args) => run_list(args).await,
    }
}

async fn run_create(args: CreateArgs) -> Result<()> {
    let tenant_id = TenantId::new(args.slug.clone())
        .with_context(|| format!("invalid tenant slug '{}'", args.slug))?;

    let data_dir = resolve_data_dir(args.data_dir.as_deref());
    std::fs::create_dir_all(&data_dir)
        .with_context(|| format!("create data dir {}", data_dir.display()))?;

    // Per-tenant dir tree must exist before any per-tenant SQLite is
    // opened (downstream stores call `tenant_db_path(...)` which
    // assumes the parent exists).
    let tenant_dir = tenant_root_dir(&data_dir, &tenant_id);
    std::fs::create_dir_all(&tenant_dir)
        .with_context(|| format!("create tenant dir {}", tenant_dir.display()))?;

    let display_name = args.display_name.unwrap_or_else(|| args.slug.clone());

    let admin_db_path = data_dir.join("tenants.sqlite");
    let db = AdminDb::open(&admin_db_path)
        .await
        .with_context(|| format!("open admin db {}", admin_db_path.display()))?;

    let now_ms = now_unix_ms();
    match db.create_tenant(&tenant_id, &display_name, now_ms).await {
        Ok(()) => {}
        Err(AdminDbError::TenantExists(slug)) => {
            bail!("tenant '{slug}' already exists in {}", admin_db_path.display());
        }
        Err(e) => return Err(anyhow!(e).context("create tenant row")),
    }

    let password = match args.admin_password {
        Some(p) => p,
        None => prompt_password(&format!(
            "admin password for tenant '{}': ",
            tenant_id.as_str()
        ))?,
    };
    if password.is_empty() {
        bail!("admin password must not be empty");
    }
    let password_hash = hash_password(&password).context("argon2id hash")?;

    match db
        .add_admin(&tenant_id, &args.admin_username, &password_hash, now_ms)
        .await
    {
        Ok(()) => {}
        Err(AdminDbError::AdminExists { tenant, username }) => {
            bail!("admin '{username}' already exists for tenant '{tenant}'");
        }
        Err(e) => return Err(anyhow!(e).context("insert admin credential")),
    }

    println!(
        "created tenant '{slug}' ({display}) with admin '{user}'",
        slug = tenant_id.as_str(),
        display = display_name,
        user = args.admin_username,
    );
    println!("  data dir : {}", tenant_dir.display());
    println!("  admin db : {}", admin_db_path.display());

    Ok(())
}

async fn run_list(args: ListArgs) -> Result<()> {
    let data_dir = resolve_data_dir(args.data_dir.as_deref());
    let admin_db_path = data_dir.join("tenants.sqlite");

    if !admin_db_path.exists() {
        println!("(no tenants.sqlite at {})", admin_db_path.display());
        println!("  run `corlinman tenant create <slug>` first");
        return Ok(());
    }

    let db = AdminDb::open(&admin_db_path)
        .await
        .with_context(|| format!("open admin db {}", admin_db_path.display()))?;

    let rows = db.list_active().await.context("list_active")?;

    if rows.is_empty() {
        println!("(no tenants registered)");
        return Ok(());
    }

    let table_rows: Vec<TenantTableRow> = rows
        .iter()
        .map(|r| TenantTableRow {
            tenant_id: r.tenant_id.as_str().to_string(),
            display_name: r.display_name.clone(),
            created_at: format_unix_ms(r.created_at),
        })
        .collect();

    let mut table = Table::new(&table_rows);
    table.with(Style::modern());
    println!("{table}");
    Ok(())
}

#[derive(Debug, Tabled)]
struct TenantTableRow {
    #[tabled(rename = "TENANT ID")]
    tenant_id: String,
    #[tabled(rename = "DISPLAY NAME")]
    display_name: String,
    #[tabled(rename = "CREATED")]
    created_at: String,
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

fn hash_password(password: &str) -> Result<String> {
    let salt = SaltString::generate(&mut OsRng);
    let hash = Argon2::default()
        .hash_password(password.as_bytes(), &salt)
        .map_err(|e| anyhow!("argon2: {e}"))?;
    Ok(hash.to_string())
}

fn prompt_password(prompt: &str) -> Result<String> {
    // Use `dialoguer::Password` when stdin is a TTY; fall back to
    // simple line read otherwise so non-interactive callers (CI,
    // scripts) can pipe a password in via stdin without the CLI
    // hanging on a non-existent TTY.
    if atty_stdin() {
        let pw = dialoguer::Password::new()
            .with_prompt(prompt.trim_end_matches([':', ' ']))
            .interact()
            .map_err(|e| anyhow!("password prompt: {e}"))?;
        Ok(pw)
    } else {
        // Non-TTY: read a single line from stdin, no echo control.
        eprint!("{prompt}");
        let _ = std::io::stderr().flush();
        let mut buf = String::new();
        std::io::stdin().read_line(&mut buf).context("read stdin")?;
        Ok(buf.trim_end_matches(['\n', '\r']).to_string())
    }
}

fn atty_stdin() -> bool {
    // Hand-rolled rather than pulling a new dep. `is_terminal` (std,
    // 1.70+) is available throughout this project's MSRV.
    use std::io::IsTerminal;
    std::io::stdin().is_terminal()
}

fn now_unix_ms() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

fn format_unix_ms(ms: i64) -> String {
    // OffsetDateTime expects nanoseconds since the epoch; convert
    // from millis. `OffsetDateTime::from_unix_timestamp_nanos` is
    // infallible for the range we care about.
    let nanos = (ms as i128) * 1_000_000;
    match time::OffsetDateTime::from_unix_timestamp_nanos(nanos) {
        Ok(dt) => dt
            .format(&time::format_description::well_known::Rfc3339)
            .unwrap_or_else(|_| ms.to_string()),
        Err(_) => ms.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn now_unix_ms_is_positive() {
        assert!(now_unix_ms() > 1_700_000_000_000);
    }

    #[test]
    fn format_unix_ms_round_trips_through_rfc3339() {
        // Pick a known instant: 2026-05-01T00:00:00Z = 1777593600000
        // (computed via `date -u -d '2026-05-01' +%s` * 1000).
        let formatted = format_unix_ms(1_777_593_600_000);
        assert!(formatted.starts_with("2026-05-01T"), "got: {formatted}");
    }

    #[tokio::test]
    async fn run_create_then_list_round_trip_via_data_dir_arg() {
        let tmp = TempDir::new().unwrap();
        let data_dir = tmp.path().to_path_buf();

        let create = CreateArgs {
            slug: "acme".into(),
            display_name: Some("Acme Corp".into()),
            data_dir: Some(data_dir.clone()),
            admin_username: "alice".into(),
            admin_password: Some("not-a-secret".into()),
        };
        run_create(create).await.unwrap();

        // Files landed at the expected paths.
        assert!(data_dir.join("tenants.sqlite").exists());
        assert!(data_dir.join("tenants").join("acme").is_dir());

        // The admin DB has the tenant + the admin row.
        let db = AdminDb::open(&data_dir.join("tenants.sqlite")).await.unwrap();
        let listed = db.list_active().await.unwrap();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].tenant_id.as_str(), "acme");
        assert_eq!(listed[0].display_name, "Acme Corp");

        let admins = db
            .list_admins(&TenantId::new("acme").unwrap())
            .await
            .unwrap();
        assert_eq!(admins.len(), 1);
        assert_eq!(admins[0].username, "alice");
        assert!(admins[0].password_hash.starts_with("$argon2id$"));
    }

    #[tokio::test]
    async fn run_create_rejects_duplicate_slug() {
        let tmp = TempDir::new().unwrap();
        let data_dir = tmp.path().to_path_buf();

        let make_args = || CreateArgs {
            slug: "acme".into(),
            display_name: None,
            data_dir: Some(data_dir.clone()),
            admin_username: "alice".into(),
            admin_password: Some("pwd".into()),
        };

        run_create(make_args()).await.unwrap();
        let err = run_create(make_args()).await.expect_err("dup must fail");
        assert!(err.to_string().contains("already exists"), "got: {err}");
    }

    #[tokio::test]
    async fn run_create_rejects_invalid_slug() {
        let tmp = TempDir::new().unwrap();
        let args = CreateArgs {
            slug: "BAD!!".into(),
            display_name: None,
            data_dir: Some(tmp.path().to_path_buf()),
            admin_username: "x".into(),
            admin_password: Some("y".into()),
        };
        let err = run_create(args).await.expect_err("bad slug must fail");
        assert!(err.to_string().contains("invalid tenant slug"), "got: {err}");
    }

    #[tokio::test]
    async fn run_create_rejects_empty_password() {
        let tmp = TempDir::new().unwrap();
        let args = CreateArgs {
            slug: "acme".into(),
            display_name: None,
            data_dir: Some(tmp.path().to_path_buf()),
            admin_username: "alice".into(),
            admin_password: Some(String::new()),
        };
        let err = run_create(args).await.expect_err("empty pw must fail");
        assert!(err.to_string().contains("must not be empty"), "got: {err}");
    }

    #[tokio::test]
    async fn run_list_handles_missing_db_gracefully() {
        let tmp = TempDir::new().unwrap();
        // No tenants.sqlite — the command should print a guidance
        // message rather than fail.
        let args = ListArgs {
            data_dir: Some(tmp.path().to_path_buf()),
        };
        run_list(args).await.unwrap();
    }
}
