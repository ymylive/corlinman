//! HTTP client for the new-api admin & runtime endpoints corlinman
//! actually consumes. Surface is intentionally small: probe + channel
//! listing + 1-token round-trip test.

use std::time::{Duration, Instant};

use reqwest::Client;
use serde::Deserialize;
use thiserror::Error;
use url::Url;

use crate::types::{Channel, ChannelType, ProbeResult, TestResult, User};

#[derive(Debug, Error)]
pub enum NewapiError {
    #[error("http request failed: {0}")]
    Http(#[from] reqwest::Error),
    #[error("invalid base url: {0}")]
    Url(#[from] url::ParseError),
    #[error("upstream returned status {status}: {body}")]
    Upstream { status: u16, body: String },
    #[error("upstream returned malformed json: {0}")]
    Json(#[from] serde_json::Error),
    #[error("upstream is not new-api (missing /api/status or wrong shape)")]
    NotNewapi,
}

#[derive(Debug, Deserialize)]
struct NewapiEnvelope<T> {
    #[allow(dead_code)]
    #[serde(default)]
    success: bool,
    data: T,
}

#[derive(Debug, Deserialize)]
struct StatusData {
    #[serde(default)]
    version: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ChatCompletionMin {
    model: String,
}

/// Client owns base_url + tokens. Cheap to clone (reqwest::Client is
/// Arc-backed). One instance per logical newapi endpoint.
#[derive(Debug, Clone)]
pub struct NewapiClient {
    base_url: Url,
    user_token: String,
    admin_token: Option<String>,
    http: Client,
}

impl NewapiClient {
    pub fn new(
        base_url: impl AsRef<str>,
        user_token: impl Into<String>,
        admin_token: Option<String>,
    ) -> Result<Self, NewapiError> {
        let http = Client::builder()
            .timeout(Duration::from_secs(8))
            .build()?;
        Ok(Self {
            base_url: Url::parse(base_url.as_ref())?,
            user_token: user_token.into(),
            admin_token,
            http,
        })
    }

    pub fn base_url(&self) -> &Url {
        &self.base_url
    }

    fn admin_or_user_token(&self) -> &str {
        self.admin_token.as_deref().unwrap_or(&self.user_token)
    }

    /// Probe a base_url + token pair. Validates (a) the user/self call
    /// succeeds with the supplied token, and (b) the host exposes
    /// `/api/status` (new-api signature). Returns the resolved user
    /// + server version. Used by both onboard step 2 and the
    /// `/admin/newapi` PATCH revalidation hook.
    pub async fn probe(&self) -> Result<ProbeResult, NewapiError> {
        let user = self.get_user_self().await?;

        let status_url = self.base_url.join("/api/status")?;
        let r = self.http.get(status_url).send().await?;
        if !r.status().is_success() {
            return Err(NewapiError::NotNewapi);
        }
        let env: NewapiEnvelope<StatusData> = r.json().await?;
        Ok(ProbeResult {
            base_url: self.base_url.to_string(),
            user,
            server_version: env.data.version,
        })
    }

    /// Retrieve the user record bound to the configured admin/user
    /// token. new-api distinguishes "user token" (sk-...) from
    /// "system access token"; both work for /api/user/self but only
    /// system tokens authorize /api/channel/. Prefer the admin token
    /// if present.
    pub async fn get_user_self(&self) -> Result<User, NewapiError> {
        let url = self.base_url.join("/api/user/self")?;
        let r = self
            .http
            .get(url)
            .bearer_auth(self.admin_or_user_token())
            .send()
            .await?;
        let status = r.status();
        if !status.is_success() {
            let body = r.text().await.unwrap_or_default();
            return Err(NewapiError::Upstream {
                status: status.as_u16(),
                body,
            });
        }
        let env: NewapiEnvelope<User> = r.json().await?;
        Ok(env.data)
    }

    /// List channels of a given type. Filters on the server side via
    /// the integer type code; corlinman re-projects to its own
    /// ChannelType enum for type safety.
    pub async fn list_channels(&self, channel_type: ChannelType) -> Result<Vec<Channel>, NewapiError> {
        let mut url = self.base_url.join("/api/channel/")?;
        url.query_pairs_mut()
            .append_pair("type", &channel_type.as_int().to_string());
        let r = self
            .http
            .get(url)
            .bearer_auth(self.admin_or_user_token())
            .send()
            .await?;
        let status = r.status();
        if !status.is_success() {
            let body = r.text().await.unwrap_or_default();
            return Err(NewapiError::Upstream {
                status: status.as_u16(),
                body,
            });
        }
        let env: NewapiEnvelope<Vec<Channel>> = r.json().await?;
        Ok(env.data)
    }

    /// 1-token chat round-trip used by /admin/newapi/test. Measures
    /// wall-clock latency from request start to header receipt.
    /// Distinct from /admin/newapi/probe — probe validates the
    /// connection, test validates an actual user-token chat path.
    pub async fn test_round_trip(&self, model: &str) -> Result<TestResult, NewapiError> {
        let url = self.base_url.join("/v1/chat/completions")?;
        let payload = serde_json::json!({
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "temperature": 0
        });
        let started = Instant::now();
        let r = self
            .http
            .post(url)
            .bearer_auth(&self.user_token)
            .json(&payload)
            .send()
            .await?;
        let latency_ms = started.elapsed().as_millis();
        let status = r.status();
        if !status.is_success() {
            let body = r.text().await.unwrap_or_default();
            return Err(NewapiError::Upstream {
                status: status.as_u16(),
                body,
            });
        }
        let parsed: ChatCompletionMin = r.json().await.unwrap_or(ChatCompletionMin {
            model: model.to_string(),
        });
        Ok(TestResult {
            status: status.as_u16(),
            latency_ms,
            model: Some(parsed.model),
        })
    }
}
