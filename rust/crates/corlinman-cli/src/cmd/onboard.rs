//! `corlinman onboard` — first-run wizard (plan §8 B5).
//!
//! Interactive mode uses `dialoguer` to collect port / admin creds / at least
//! one provider API key. Non-interactive mode (CI / Dockerfile bake) takes
//! defaults plus env overrides; it requires `--accept-risk` to acknowledge
//! that skipping prompts produces a config that still fails `corlinman config
//! validate` (no provider configured → validation issue).
//!
//! Regardless of mode, `onboard` materialises the data-dir skeleton:
//!
//! ```text
//! ~/.corlinman/
//! ├── config.toml
//! ├── agents/
//! ├── plugins/
//! ├── knowledge/
//! ├── vector/
//! └── logs/
//! ```

use std::path::{Path, PathBuf};

use anyhow::{anyhow, Context, Result};
use argon2::password_hash::rand_core::OsRng;
use argon2::password_hash::SaltString;
use argon2::{Argon2, PasswordHasher};
use clap::Parser;
use corlinman_core::config::{Config, ProviderEntry, SecretRef};
use dialoguer::{theme::ColorfulTheme, Confirm, Input, Password, Select};

const SUBDIRS: &[&str] = &["agents", "plugins", "knowledge", "vector", "logs"];

#[derive(Debug, Parser)]
pub struct Args {
    /// Skip prompts; use defaults + env vars.
    #[arg(long)]
    pub non_interactive: bool,

    /// Acknowledge that some checks will be skipped in non-interactive mode.
    #[arg(long)]
    pub accept_risk: bool,

    /// Override data-dir (default: `$CORLINMAN_DATA_DIR` or `~/.corlinman`).
    #[arg(long)]
    pub data_dir: Option<PathBuf>,

    /// Overwrite an existing `config.toml`.
    #[arg(long)]
    pub force: bool,
}

pub async fn run(args: Args) -> Result<()> {
    let data_dir = resolve_data_dir(args.data_dir.clone());
    let config_path = data_dir.join("config.toml");

    if config_path.exists() && !args.force {
        return Err(anyhow!(
            "{} already exists; pass --force to overwrite",
            config_path.display()
        ));
    }

    if args.non_interactive && !args.accept_risk {
        return Err(anyhow!(
            "--non-interactive requires --accept-risk (no provider will be configured)"
        ));
    }

    let cfg = if args.non_interactive {
        non_interactive_config(&data_dir)
    } else {
        interactive_config(&data_dir)?
    };

    ensure_skeleton(&data_dir)?;
    cfg.save_to_path(&config_path)
        .with_context(|| format!("write {}", config_path.display()))?;

    println!("onboard complete:");
    println!("  data_dir : {}", data_dir.display());
    println!("  config   : {}", config_path.display());
    let issues = cfg.validate_report();
    if issues.is_empty() {
        println!("  status   : OK");
    } else {
        println!(
            "  status   : {} issue(s) — run `corlinman config validate` for details",
            issues.len()
        );
    }
    Ok(())
}

fn resolve_data_dir(explicit: Option<PathBuf>) -> PathBuf {
    if let Some(p) = explicit {
        return p;
    }
    if let Some(env) = std::env::var_os(corlinman_core::config::ENV_DATA_DIR) {
        return PathBuf::from(env);
    }
    dirs::home_dir().unwrap_or_default().join(".corlinman")
}

fn ensure_skeleton(data_dir: &Path) -> Result<()> {
    std::fs::create_dir_all(data_dir).with_context(|| format!("create {}", data_dir.display()))?;
    for sub in SUBDIRS {
        std::fs::create_dir_all(data_dir.join(sub))
            .with_context(|| format!("create {}/{}", data_dir.display(), sub))?;
    }
    Ok(())
}

fn non_interactive_config(data_dir: &Path) -> Config {
    let mut cfg = Config::default();
    cfg.server.data_dir = data_dir.to_path_buf();
    cfg
}

fn interactive_config(data_dir: &Path) -> Result<Config> {
    let theme = ColorfulTheme::default();
    println!("corlinman onboard — answer a few questions (Ctrl+C to abort)");

    let port: u16 = Input::with_theme(&theme)
        .with_prompt("HTTP port")
        .default(6005u16)
        .interact_text()?;

    let bind: String = Input::with_theme(&theme)
        .with_prompt("Bind address")
        .default("0.0.0.0".to_string())
        .interact_text()?;

    let admin_user: String = Input::with_theme(&theme)
        .with_prompt("Admin username")
        .default("admin".to_string())
        .interact_text()?;

    let password_hash = if Confirm::with_theme(&theme)
        .with_prompt("Set an admin password now? (argon2id)")
        .default(true)
        .interact()?
    {
        let pw = Password::with_theme(&theme)
            .with_prompt("Admin password")
            .with_confirmation("Confirm password", "Passwords do not match")
            .interact()?;
        Some(hash_password(&pw)?)
    } else {
        None
    };

    // Provider selection — user must pick at least one.
    const PROVIDERS: &[&str] = &["anthropic", "openai", "google", "deepseek", "qwen", "glm"];
    let idx = Select::with_theme(&theme)
        .with_prompt("Pick a primary LLM provider")
        .items(PROVIDERS)
        .default(0)
        .interact()?;
    let provider_name = PROVIDERS[idx];

    let default_env = match provider_name {
        "anthropic" => "ANTHROPIC_API_KEY",
        "openai" => "OPENAI_API_KEY",
        "google" => "GOOGLE_API_KEY",
        "deepseek" => "DEEPSEEK_API_KEY",
        "qwen" => "DASHSCOPE_API_KEY",
        "glm" => "ZHIPUAI_API_KEY",
        _ => "API_KEY",
    };
    let env_var: String = Input::with_theme(&theme)
        .with_prompt(format!(
            "Env var holding the {provider_name} API key (value read at startup)"
        ))
        .default(default_env.to_string())
        .interact_text()?;

    // Build config.
    let mut cfg = Config::default();
    cfg.server.port = port;
    cfg.server.bind = bind;
    cfg.server.data_dir = data_dir.to_path_buf();
    cfg.admin.username = Some(admin_user);
    cfg.admin.password_hash = password_hash;

    let entry = ProviderEntry {
        api_key: Some(SecretRef::EnvVar { env: env_var }),
        base_url: None,
        enabled: true,
    };
    match provider_name {
        "anthropic" => cfg.providers.anthropic = Some(entry),
        "openai" => cfg.providers.openai = Some(entry),
        "google" => cfg.providers.google = Some(entry),
        "deepseek" => cfg.providers.deepseek = Some(entry),
        "qwen" => cfg.providers.qwen = Some(entry),
        "glm" => cfg.providers.glm = Some(entry),
        _ => {}
    }

    Ok(cfg)
}

fn hash_password(pw: &str) -> Result<String> {
    let salt = SaltString::generate(&mut OsRng);
    let hash = Argon2::default()
        .hash_password(pw.as_bytes(), &salt)
        .map_err(|e| anyhow!("argon2 hash failed: {e}"))?
        .to_string();
    Ok(hash)
}
