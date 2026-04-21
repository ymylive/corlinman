//! TCP smoke test for each enabled provider's base URL.
//!
//! We only TCP-connect to the host:port of the provider's `base_url` (or
//! its documented default) — no HTTP handshake. This catches DNS / firewall
//! issues without spending a token-costing request.
//!
//! Result is a `Warn` on any failure — missing Internet access shouldn't
//! hard-fail `doctor` on a laptop running offline.

use std::time::Duration;

use async_trait::async_trait;
use tokio::time::timeout;

use super::{DoctorCheck, DoctorContext, DoctorResult};

pub struct ProviderHttpsSmokeCheck;

impl ProviderHttpsSmokeCheck {
    pub fn new() -> Self {
        Self
    }
}

impl Default for ProviderHttpsSmokeCheck {
    fn default() -> Self {
        Self::new()
    }
}

/// Documented defaults. Mirrors `corlinman-providers` wiring.
fn default_host(name: &str) -> Option<&'static str> {
    match name {
        "openai" => Some("api.openai.com"),
        "anthropic" => Some("api.anthropic.com"),
        "google" => Some("generativelanguage.googleapis.com"),
        "deepseek" => Some("api.deepseek.com"),
        "qwen" => Some("dashscope.aliyuncs.com"),
        "glm" => Some("open.bigmodel.cn"),
        _ => None,
    }
}

/// Pull `host[:port]` from a base URL, falling back to the documented
/// default for `name` and port 443.
fn host_port(name: &str, base_url: Option<&str>) -> Option<String> {
    let raw = base_url.and_then(|u| url::Url::parse(u).ok());
    let host = raw
        .as_ref()
        .and_then(|u| u.host_str())
        .map(str::to_string)
        .or_else(|| default_host(name).map(str::to_string))?;
    let port = raw.as_ref().and_then(|u| u.port()).unwrap_or(443);
    Some(format!("{host}:{port}"))
}

#[async_trait]
impl DoctorCheck for ProviderHttpsSmokeCheck {
    fn name(&self) -> &str {
        "provider_https_smoke"
    }

    async fn run(&self, ctx: &DoctorContext) -> DoctorResult {
        let Some(cfg) = ctx.config.as_ref() else {
            return DoctorResult::Warn {
                message: "skipped: config not loaded".into(),
                hint: Some("fix the config check first".into()),
            };
        };
        let enabled: Vec<(&'static str, Option<&str>)> = cfg
            .providers
            .iter()
            .filter(|(_, e)| e.enabled)
            .map(|(name, e)| (name, e.base_url.as_deref()))
            .collect();

        if enabled.is_empty() {
            return DoctorResult::Ok {
                message: "no enabled providers".into(),
            };
        }

        let mut failures: Vec<String> = Vec::new();
        let mut ok_hosts: Vec<String> = Vec::new();
        for (name, base) in enabled {
            let Some(addr) = host_port(name, base) else {
                failures.push(format!("{name}: base_url parse failed"));
                continue;
            };
            match timeout(
                Duration::from_millis(800),
                tokio::net::TcpStream::connect(&addr),
            )
            .await
            {
                Ok(Ok(_)) => ok_hosts.push(addr),
                Ok(Err(e)) => failures.push(format!("{name}:{addr} {e}")),
                Err(_) => failures.push(format!("{name}:{addr} timed out")),
            }
        }
        if failures.is_empty() {
            DoctorResult::Ok {
                message: format!("{} provider host(s) reachable", ok_hosts.len()),
            }
        } else {
            DoctorResult::Warn {
                message: failures.join("; "),
                hint: Some("doctor runs offline-safe; this is informational".into()),
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

    #[test]
    fn host_port_uses_provider_default() {
        assert_eq!(
            host_port("openai", None).as_deref(),
            Some("api.openai.com:443")
        );
        assert_eq!(
            host_port("anthropic", Some("https://proxy.example.com/v1")).as_deref(),
            Some("proxy.example.com:443"),
        );
        assert_eq!(
            host_port("openai", Some("https://localhost:1234")).as_deref(),
            Some("localhost:1234"),
        );
        assert_eq!(host_port("unknown", None), None);
    }

    #[tokio::test]
    async fn no_enabled_providers_is_ok() {
        let ctx = ctx_with(Some(Config::default()));
        let res = ProviderHttpsSmokeCheck::new().run(&ctx).await;
        assert_eq!(res.status_str(), "ok", "got: {res:?}");
    }

    #[tokio::test]
    async fn unreachable_base_url_is_warn() {
        let mut cfg = Config::default();
        cfg.providers.openai = Some(ProviderEntry {
            api_key: Some(SecretRef::Literal { value: "x".into() }),
            base_url: Some("https://127.0.0.1:1/v1".into()),
            enabled: true,
            ..Default::default()
        });
        let ctx = ctx_with(Some(cfg));
        let res = ProviderHttpsSmokeCheck::new().run(&ctx).await;
        assert_eq!(res.status_str(), "warn", "got: {res:?}");
    }
}
