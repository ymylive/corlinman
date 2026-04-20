//! Upstream check: confirm that every enabled provider can resolve its
//! `api_key` secret from the environment.
//!
//! We intentionally *don't* issue a real HTTPS round-trip here — that belongs
//! in a separate opt-in smoke test. `doctor` is expected to be fast and
//! offline-safe; what goes wrong most often in practice is an unset env var,
//! which we can catch without touching the network.

use async_trait::async_trait;

use super::{DoctorCheck, DoctorContext, DoctorResult};

pub struct UpstreamCheck;

impl UpstreamCheck {
    pub fn new() -> Self {
        Self
    }
}

impl Default for UpstreamCheck {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl DoctorCheck for UpstreamCheck {
    fn name(&self) -> &str {
        "upstream"
    }

    async fn run(&self, ctx: &DoctorContext) -> DoctorResult {
        let Some(cfg) = ctx.config.as_ref() else {
            return DoctorResult::Warn {
                message: "skipped: config not loaded".into(),
                hint: Some("fix the config check first".into()),
            };
        };

        let enabled: Vec<_> = cfg.providers.iter().filter(|(_, e)| e.enabled).collect();

        if enabled.is_empty() {
            return DoctorResult::Warn {
                message: "no provider is enabled".into(),
                hint: Some("enable a [providers.*] entry in config.toml".into()),
            };
        }

        let mut failures: Vec<String> = Vec::new();
        let mut ok_names: Vec<&'static str> = Vec::new();
        for (name, entry) in &enabled {
            match entry.api_key.as_ref() {
                None => failures.push(format!("{name}: missing api_key")),
                Some(secret) => match secret.resolve() {
                    Ok(_) => ok_names.push(name),
                    Err(e) => failures.push(format!("{name}: {e}")),
                },
            }
        }

        if failures.is_empty() {
            DoctorResult::Ok {
                message: format!(
                    "{} provider(s) reachable: {}",
                    ok_names.len(),
                    ok_names.join(", ")
                ),
            }
        } else {
            let first = &failures[0];
            let more = if failures.len() > 1 {
                format!(" (+{} more)", failures.len() - 1)
            } else {
                String::new()
            };
            DoctorResult::Fail {
                message: format!("{first}{more}"),
                hint: Some("export the referenced env var, or switch to { value = ... }".into()),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use corlinman_core::config::{Config, ProviderEntry, SecretRef};

    fn ctx_with(config: Option<Config>) -> DoctorContext {
        DoctorContext {
            data_dir: std::path::PathBuf::from("/tmp"),
            config_path: std::path::PathBuf::from("/tmp/config.toml"),
            config,
        }
    }

    #[tokio::test]
    async fn no_config_is_warn() {
        let ctx = ctx_with(None);
        let res = UpstreamCheck::new().run(&ctx).await;
        assert_eq!(res.status_str(), "warn");
    }

    #[tokio::test]
    async fn no_enabled_provider_is_warn() {
        let ctx = ctx_with(Some(Config::default()));
        let res = UpstreamCheck::new().run(&ctx).await;
        assert_eq!(res.status_str(), "warn");
    }

    #[tokio::test]
    async fn literal_secret_resolves_to_ok() {
        let mut cfg = Config::default();
        cfg.providers.openai = Some(ProviderEntry {
            api_key: Some(SecretRef::Literal {
                value: "sk-test".into(),
            }),
            base_url: None,
            enabled: true,
        });
        let ctx = ctx_with(Some(cfg));
        let res = UpstreamCheck::new().run(&ctx).await;
        assert_eq!(res.status_str(), "ok", "got: {:?}", res);
    }

    #[tokio::test]
    async fn missing_env_var_is_fail() {
        let mut cfg = Config::default();
        cfg.providers.anthropic = Some(ProviderEntry {
            api_key: Some(SecretRef::EnvVar {
                env: "CORLINMAN_DOCTOR_TEST_UNSET_KEY".into(),
            }),
            base_url: None,
            enabled: true,
        });
        // SAFETY: test-only env clear, no threads racing on this name.
        unsafe { std::env::remove_var("CORLINMAN_DOCTOR_TEST_UNSET_KEY") };
        let ctx = ctx_with(Some(cfg));
        let res = UpstreamCheck::new().run(&ctx).await;
        assert_eq!(res.status_str(), "fail", "got: {:?}", res);
    }
}
