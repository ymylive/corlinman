//! HTTP-backed [`MemoryHost`].
//!
//! Speaks a minimal JSON protocol:
//!
//! ```text
//! POST  {base}/query   →  {"hits": [{"id":..., "content":..., "score":..., "metadata":...}]}
//! POST  {base}/upsert  →  {"id": "..."}
//! DELETE {base}/docs/{id}
//! GET   {base}/health  →  2xx OK
//! ```
//!
//! The request body for `/query` is [`MemoryQuery`] serialised
//! verbatim; the body for `/upsert` is [`MemoryDoc`] serialised
//! verbatim. A caller-supplied bearer token is sent as
//! `Authorization: Bearer <token>` when present.

use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use async_trait::async_trait;
use reqwest::Client;
use serde::Deserialize;

use crate::{HealthStatus, MemoryDoc, MemoryHit, MemoryHost, MemoryQuery};

/// Default request timeout — intentionally hardcoded for the skeleton;
/// made configurable in Phase 2.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(5);

/// HTTP-backed memory host.
pub struct RemoteHttpHost {
    name: String,
    base_url: String,
    token: Option<String>,
    client: Client,
}

impl RemoteHttpHost {
    /// Construct a host pointing at `base_url` (no trailing slash
    /// required; the impl joins with `/query` etc).
    ///
    /// `token` is optional; when `Some`, it's sent as a bearer token
    /// on every request.
    pub fn new(
        name: impl Into<String>,
        base_url: impl Into<String>,
        token: Option<String>,
    ) -> Result<Self> {
        let client = Client::builder()
            .timeout(REQUEST_TIMEOUT)
            .build()
            .context("build reqwest client")?;
        Ok(Self {
            name: name.into(),
            base_url: base_url.into().trim_end_matches('/').to_string(),
            token,
            client,
        })
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.base_url, path)
    }

    fn auth(&self, rb: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        match &self.token {
            Some(t) => rb.bearer_auth(t),
            None => rb,
        }
    }
}

#[derive(Debug, Deserialize)]
struct QueryResponse {
    hits: Vec<RemoteHit>,
}

#[derive(Debug, Deserialize)]
struct RemoteHit {
    id: String,
    content: String,
    score: f32,
    #[serde(default)]
    metadata: serde_json::Value,
}

#[derive(Debug, Deserialize)]
struct UpsertResponse {
    id: String,
}

#[async_trait]
impl MemoryHost for RemoteHttpHost {
    fn name(&self) -> &str {
        &self.name
    }

    async fn query(&self, req: MemoryQuery) -> Result<Vec<MemoryHit>> {
        let resp = self
            .auth(self.client.post(self.url("/query")).json(&req))
            .send()
            .await
            .with_context(|| format!("POST {}/query", self.base_url))?;

        let status = resp.status();
        if !status.is_success() {
            let body = resp.text().await.unwrap_or_default();
            return Err(anyhow!(
                "RemoteHttpHost {}: query HTTP {status}: {body}",
                self.name
            ));
        }

        let parsed: QueryResponse = resp
            .json()
            .await
            .with_context(|| format!("parse query response from {}", self.name))?;

        let source = self.name.clone();
        Ok(parsed
            .hits
            .into_iter()
            .map(|h| MemoryHit {
                id: h.id,
                content: h.content,
                score: h.score,
                source: source.clone(),
                metadata: h.metadata,
            })
            .collect())
    }

    async fn upsert(&self, doc: MemoryDoc) -> Result<String> {
        let resp = self
            .auth(self.client.post(self.url("/upsert")).json(&doc))
            .send()
            .await
            .with_context(|| format!("POST {}/upsert", self.base_url))?;

        let status = resp.status();
        if !status.is_success() {
            let body = resp.text().await.unwrap_or_default();
            return Err(anyhow!(
                "RemoteHttpHost {}: upsert HTTP {status}: {body}",
                self.name
            ));
        }
        let parsed: UpsertResponse = resp
            .json()
            .await
            .with_context(|| format!("parse upsert response from {}", self.name))?;
        Ok(parsed.id)
    }

    async fn delete(&self, id: &str) -> Result<()> {
        let path = format!("/docs/{id}");
        let resp = self
            .auth(self.client.delete(self.url(&path)))
            .send()
            .await
            .with_context(|| format!("DELETE {}{}", self.base_url, path))?;
        let status = resp.status();
        if !status.is_success() {
            let body = resp.text().await.unwrap_or_default();
            return Err(anyhow!(
                "RemoteHttpHost {}: delete HTTP {status}: {body}",
                self.name
            ));
        }
        Ok(())
    }

    async fn health(&self) -> HealthStatus {
        match self.auth(self.client.get(self.url("/health"))).send().await {
            Ok(r) if r.status().is_success() => HealthStatus::Ok,
            Ok(r) => HealthStatus::Degraded(format!("HTTP {}", r.status())),
            Err(e) => HealthStatus::Down(e.to_string()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use wiremock::matchers::{body_partial_json, header, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    #[tokio::test]
    async fn query_sends_expected_body_and_parses_response() {
        let server = MockServer::start().await;

        Mock::given(method("POST"))
            .and(path("/query"))
            .and(header("authorization", "Bearer secret-token"))
            .and(body_partial_json(json!({"text": "alpha", "top_k": 5})))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "hits": [
                    {"id": "r1", "content": "alpha", "score": 0.9, "metadata": {"k": 1}},
                    {"id": "r2", "content": "beta", "score": 0.4, "metadata": {}},
                ]
            })))
            .expect(1)
            .mount(&server)
            .await;

        let host = RemoteHttpHost::new("remote", server.uri(), Some("secret-token".into()))
            .expect("build host");

        let hits = host
            .query(MemoryQuery {
                text: "alpha".into(),
                top_k: 5,
                filters: vec![],
                namespace: None,
            })
            .await
            .expect("query ok");

        assert_eq!(hits.len(), 2);
        assert_eq!(hits[0].id, "r1");
        assert_eq!(hits[0].source, "remote");
        assert_eq!(hits[0].score, 0.9);
        assert_eq!(hits[0].metadata["k"], 1);
    }

    #[tokio::test]
    async fn upsert_returns_host_assigned_id() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/upsert"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id": "remote-42"})))
            .mount(&server)
            .await;

        let host = RemoteHttpHost::new("remote", server.uri(), None).unwrap();
        let id = host
            .upsert(MemoryDoc {
                content: "c".into(),
                metadata: json!({}),
                namespace: None,
            })
            .await
            .unwrap();
        assert_eq!(id, "remote-42");
    }

    #[tokio::test]
    async fn query_http_error_propagates() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/query"))
            .respond_with(ResponseTemplate::new(500).set_body_string("kaboom"))
            .mount(&server)
            .await;

        let host = RemoteHttpHost::new("remote", server.uri(), None).unwrap();
        let err = host
            .query(MemoryQuery {
                text: "x".into(),
                top_k: 3,
                filters: vec![],
                namespace: None,
            })
            .await
            .expect_err("should error");
        let msg = format!("{err:#}");
        assert!(msg.contains("HTTP 500"), "got: {msg}");
    }

    #[tokio::test]
    async fn health_maps_status_codes() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/health"))
            .respond_with(ResponseTemplate::new(200))
            .mount(&server)
            .await;
        let host = RemoteHttpHost::new("remote", server.uri(), None).unwrap();
        assert_eq!(host.health().await, HealthStatus::Ok);
    }
}
