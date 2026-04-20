//! Config check: locate `config.toml`, parse it, run the cross-field validator.
//!
//! Three terminal states:
//!   * `Fail` — file missing, parse error, or validator produced issues.
//!   * `Warn` — file present but using all-defaults (no providers).
//!   * `Ok`   — file present, parses, validator returns zero issues.

use async_trait::async_trait;
use corlinman_core::config::Config;

use super::{DoctorCheck, DoctorContext, DoctorResult};

pub struct ConfigCheck;

impl ConfigCheck {
    pub fn new() -> Self {
        Self
    }
}

impl Default for ConfigCheck {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl DoctorCheck for ConfigCheck {
    fn name(&self) -> &str {
        "config"
    }

    async fn run(&self, ctx: &DoctorContext) -> DoctorResult {
        let path = &ctx.config_path;
        if !path.exists() {
            return DoctorResult::Fail {
                message: format!("no config file at {}", path.display()),
                hint: Some(
                    "run `corlinman onboard` to create one, or set CORLINMAN_DATA_DIR".into(),
                ),
            };
        }

        // Prefer the context-carried parsed config (runner already loaded it);
        // fall back to re-loading for the standalone-test path where callers
        // construct a DoctorContext with config = None by design.
        let cfg_owned: Config;
        let cfg: &Config = match ctx.config.as_ref() {
            Some(c) => c,
            None => match Config::load_from_path(path) {
                Ok(c) => {
                    cfg_owned = c;
                    &cfg_owned
                }
                Err(e) => {
                    return DoctorResult::Fail {
                        message: format!("failed to parse {}: {e}", path.display()),
                        hint: Some("run `corlinman config validate` for details".into()),
                    };
                }
            },
        };

        let issues = cfg.validate_report();
        if issues.is_empty() {
            return DoctorResult::Ok {
                message: format!("loaded from {}", path.display()),
            };
        }

        // Distinguish the "brand new config, no provider yet" common case from
        // real misconfiguration — the former is a warn, the latter a fail.
        let only_missing_provider = issues.len() == 1 && issues[0].code == "no_provider_enabled";
        if only_missing_provider {
            DoctorResult::Warn {
                message: format!("{} has no enabled provider", path.display()),
                hint: Some("add a [providers.*] entry with enabled = true and api_key".into()),
            }
        } else {
            let first = &issues[0];
            let more = if issues.len() > 1 {
                format!(" (+{} more)", issues.len() - 1)
            } else {
                String::new()
            };
            DoctorResult::Fail {
                message: format!("{}: {}{}", first.path, first.message, more),
                hint: Some("run `corlinman config validate` for the full list".into()),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use tempfile::tempdir;

    fn ctx_for(path: PathBuf) -> DoctorContext {
        DoctorContext {
            data_dir: path.parent().unwrap().to_path_buf(),
            config: None,
            config_path: path,
        }
    }

    #[tokio::test]
    async fn missing_config_is_fail() {
        let dir = tempdir().unwrap();
        let ctx = ctx_for(dir.path().join("nope.toml"));
        let res = ConfigCheck::new().run(&ctx).await;
        assert!(matches!(res, DoctorResult::Fail { .. }));
        assert_eq!(res.status_str(), "fail");
    }

    #[tokio::test]
    async fn empty_provider_is_warn() {
        let dir = tempdir().unwrap();
        let p = dir.path().join("config.toml");
        std::fs::write(&p, "").unwrap();
        let ctx = ctx_for(p);
        let res = ConfigCheck::new().run(&ctx).await;
        assert_eq!(res.status_str(), "warn", "got: {:?}", res);
    }

    #[tokio::test]
    async fn valid_config_is_ok() {
        let dir = tempdir().unwrap();
        let p = dir.path().join("config.toml");
        std::fs::write(
            &p,
            r#"
[providers.anthropic]
api_key = { env = "ANTHROPIC_API_KEY" }
enabled = true
"#,
        )
        .unwrap();
        let ctx = ctx_for(p);
        let res = ConfigCheck::new().run(&ctx).await;
        assert_eq!(res.status_str(), "ok", "got: {:?}", res);
    }
}
