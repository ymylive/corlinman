//! Channels-connectivity check.
//!
//! Today only `[channels.qq]` has a connectivity leg. When it's enabled we:
//!   * parse `ws_url` as a valid URL,
//!   * attempt a short WebSocket handshake (2 s timeout),
//!   * report `Warn` (not `Fail`) on connection failure so a transient gocq
//!     outage doesn't sink `doctor`.
//!
//! When no channel is enabled we return `Ok` with a neutral message — empty
//! channels config is a valid deployment (HTTP-only).

use std::time::Duration;

use async_trait::async_trait;
use tokio::time::timeout;
use tokio_tungstenite::connect_async;

use super::{DoctorCheck, DoctorContext, DoctorResult};

pub struct ChannelsCheck;

impl ChannelsCheck {
    pub fn new() -> Self {
        Self
    }
}

impl Default for ChannelsCheck {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl DoctorCheck for ChannelsCheck {
    fn name(&self) -> &str {
        "channels"
    }

    async fn run(&self, ctx: &DoctorContext) -> DoctorResult {
        let Some(cfg) = ctx.config.as_ref() else {
            return DoctorResult::Warn {
                message: "skipped: config not loaded".into(),
                hint: Some("fix the config check first".into()),
            };
        };

        let Some(qq) = cfg.channels.qq.as_ref() else {
            return DoctorResult::Ok {
                message: "no channels enabled".into(),
            };
        };

        if !qq.enabled {
            return DoctorResult::Ok {
                message: "channels.qq declared but not enabled".into(),
            };
        }

        // URL parse check.
        let url = match url::Url::parse(&qq.ws_url) {
            Ok(u) => u,
            Err(e) => {
                return DoctorResult::Fail {
                    message: format!("channels.qq.ws_url invalid: {e}"),
                    hint: Some("expected ws:// or wss:// scheme with host:port".into()),
                }
            }
        };
        if !matches!(url.scheme(), "ws" | "wss") {
            return DoctorResult::Fail {
                message: format!(
                    "channels.qq.ws_url scheme must be ws/wss, got {}",
                    url.scheme()
                ),
                hint: Some("fix the URL in config.toml".into()),
            };
        }

        // Short handshake probe (non-fatal on failure).
        match timeout(Duration::from_secs(2), connect_async(qq.ws_url.as_str())).await {
            Ok(Ok((_stream, _resp))) => DoctorResult::Ok {
                message: format!("channels.qq ws handshake ok ({})", qq.ws_url),
            },
            Ok(Err(e)) => DoctorResult::Warn {
                message: format!("channels.qq ws unreachable: {e}"),
                hint: Some(
                    "verify gocq/NapCatQQ is running and ws_url matches its bind address".into(),
                ),
            },
            Err(_) => DoctorResult::Warn {
                message: format!("channels.qq ws connect timed out (2s) to {}", qq.ws_url),
                hint: Some("the bot process may be down or unreachable".into()),
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use corlinman_core::config::{Config, QqChannelConfig};
    use std::collections::HashMap;

    fn ctx_with(config: Option<Config>) -> DoctorContext {
        DoctorContext {
            data_dir: std::path::PathBuf::from("/tmp"),
            config_path: std::path::PathBuf::from("/tmp/config.toml"),
            config,
        }
    }

    #[tokio::test]
    async fn no_channels_is_ok() {
        let ctx = ctx_with(Some(Config::default()));
        let res = ChannelsCheck::new().run(&ctx).await;
        assert_eq!(res.status_str(), "ok", "got: {:?}", res);
    }

    #[tokio::test]
    async fn disabled_channel_is_ok() {
        let mut cfg = Config::default();
        cfg.channels.qq = Some(QqChannelConfig {
            enabled: false,
            ws_url: "ws://127.0.0.1:1/x".into(),
            access_token: None,
            self_ids: vec![],
            group_keywords: HashMap::new(),
            rate_limit: Default::default(),
        });
        let ctx = ctx_with(Some(cfg));
        let res = ChannelsCheck::new().run(&ctx).await;
        assert_eq!(res.status_str(), "ok");
    }

    #[tokio::test]
    async fn invalid_ws_url_is_fail() {
        let mut cfg = Config::default();
        cfg.channels.qq = Some(QqChannelConfig {
            enabled: true,
            ws_url: "not a url at all".into(),
            access_token: None,
            self_ids: vec![1],
            group_keywords: HashMap::new(),
            rate_limit: Default::default(),
        });
        let ctx = ctx_with(Some(cfg));
        let res = ChannelsCheck::new().run(&ctx).await;
        assert_eq!(res.status_str(), "fail", "got: {:?}", res);
    }

    #[tokio::test]
    async fn unreachable_ws_is_warn() {
        // 127.0.0.1 on a reserved/high port — will be ConnRefused/timeout.
        let mut cfg = Config::default();
        cfg.channels.qq = Some(QqChannelConfig {
            enabled: true,
            ws_url: "ws://127.0.0.1:1/".into(),
            access_token: None,
            self_ids: vec![1],
            group_keywords: HashMap::new(),
            rate_limit: Default::default(),
        });
        let ctx = ctx_with(Some(cfg));
        let res = ChannelsCheck::new().run(&ctx).await;
        assert_eq!(res.status_str(), "warn", "got: {:?}", res);
    }
}
