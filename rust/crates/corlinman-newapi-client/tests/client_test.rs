//! Wiremock-driven tests for the corlinman-newapi-client surface.
//!
//! Covers: probe (happy + 401 + non-newapi), get_user_self (separately),
//! list_channels (filter + empty), test_round_trip (latency + 4xx).

use corlinman_newapi_client::{ChannelType, NewapiClient, NewapiError};
use serde_json::json;
use wiremock::matchers::{header, method, path, query_param};
use wiremock::{Mock, MockServer, ResponseTemplate};

// -- probe -----------------------------------------------------------

#[tokio::test]
async fn probe_returns_user_when_200() {
    let server = MockServer::start().await;

    Mock::given(method("GET"))
        .and(path("/api/user/self"))
        .and(header("Authorization", "Bearer admin-tok"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "success": true,
            "data": {
                "id": 1, "username": "root", "display_name": "Root",
                "role": 100, "status": 1
            }
        })))
        .mount(&server)
        .await;

    Mock::given(method("GET"))
        .and(path("/api/status"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "success": true, "data": { "version": "v0.4.0" }
        })))
        .mount(&server)
        .await;

    let client = NewapiClient::new(server.uri(), "user-tok", Some("admin-tok".into())).unwrap();
    let result = client.probe().await.unwrap();
    assert_eq!(result.user.username, "root");
    assert_eq!(result.server_version.as_deref(), Some("v0.4.0"));
}

#[tokio::test]
async fn probe_returns_unauthorized_on_401() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/api/user/self"))
        .respond_with(ResponseTemplate::new(401).set_body_string("unauthorized"))
        .mount(&server)
        .await;

    let client = NewapiClient::new(server.uri(), "bad", None).unwrap();
    let err = client.probe().await.unwrap_err();
    assert!(
        matches!(err, NewapiError::Upstream { status: 401, .. }),
        "expected Upstream{{401}}, got: {err:?}"
    );
}

#[tokio::test]
async fn probe_returns_notnewapi_when_status_endpoint_missing() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/api/user/self"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "success": true,
            "data": { "id": 1, "username": "x", "role": 1, "status": 1 }
        })))
        .mount(&server)
        .await;
    Mock::given(method("GET"))
        .and(path("/api/status"))
        .respond_with(ResponseTemplate::new(404))
        .mount(&server)
        .await;

    let client = NewapiClient::new(server.uri(), "tok", None).unwrap();
    let err = client.probe().await.unwrap_err();
    assert!(
        matches!(err, NewapiError::NotNewapi),
        "expected NotNewapi, got: {err:?}"
    );
}

// -- list_channels ---------------------------------------------------

#[tokio::test]
async fn list_channels_returns_filtered_by_type() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/api/channel/"))
        .and(query_param("type", "1"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "success": true,
            "data": [
                { "id": 10, "name": "openai-primary", "type": 1, "status": 1,
                  "models": "gpt-4o,gpt-4o-mini", "group": "default" },
                { "id": 11, "name": "openai-fallback", "type": 1, "status": 2,
                  "models": "gpt-4o", "group": "default" }
            ]
        })))
        .mount(&server)
        .await;

    let client = NewapiClient::new(server.uri(), "tok", None).unwrap();
    let channels = client.list_channels(ChannelType::Llm).await.unwrap();
    assert_eq!(channels.len(), 2);
    assert_eq!(channels[0].name, "openai-primary");
    assert!(channels[0].models.contains("gpt-4o"));
}

#[tokio::test]
async fn list_channels_returns_empty_on_empty_data() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/api/channel/"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "success": true, "data": []
        })))
        .mount(&server)
        .await;
    let client = NewapiClient::new(server.uri(), "tok", None).unwrap();
    let channels = client.list_channels(ChannelType::Embedding).await.unwrap();
    assert!(channels.is_empty());
}

#[tokio::test]
async fn list_channels_filters_embedding_with_type_2() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/api/channel/"))
        .and(query_param("type", "2"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "success": true,
            "data": [
                { "id": 20, "name": "emb-bge", "type": 2, "status": 1,
                  "models": "BAAI/bge-large-zh-v1.5", "group": "default" }
            ]
        })))
        .mount(&server)
        .await;
    let client = NewapiClient::new(server.uri(), "tok", None).unwrap();
    let channels = client.list_channels(ChannelType::Embedding).await.unwrap();
    assert_eq!(channels.len(), 1);
    assert_eq!(channels[0].channel_type, 2);
}

// -- test_round_trip -------------------------------------------------

#[tokio::test]
async fn test_round_trip_records_latency() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/chat/completions"))
        .and(header("Authorization", "Bearer user-tok"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "chatcmpl-1", "object": "chat.completion",
            "model": "gpt-4o-mini",
            "choices": [{
                "index": 0,
                "message": { "role": "assistant", "content": "ok" },
                "finish_reason": "stop"
            }]
        })))
        .mount(&server)
        .await;
    let client = NewapiClient::new(server.uri(), "user-tok", None).unwrap();
    let res = client.test_round_trip("gpt-4o-mini").await.unwrap();
    assert_eq!(res.status, 200);
    assert!(res.latency_ms < 5000);
    assert_eq!(res.model.as_deref(), Some("gpt-4o-mini"));
}

#[tokio::test]
async fn test_round_trip_propagates_4xx() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/chat/completions"))
        .respond_with(ResponseTemplate::new(429).set_body_string("rate limited"))
        .mount(&server)
        .await;
    let client = NewapiClient::new(server.uri(), "t", None).unwrap();
    let err = client.test_round_trip("x").await.unwrap_err();
    assert!(
        matches!(err, NewapiError::Upstream { status: 429, .. }),
        "expected Upstream{{429}}, got: {err:?}"
    );
}

// -- get_user_self (separate from probe) -----------------------------

#[tokio::test]
async fn get_user_self_uses_admin_token_when_present() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/api/user/self"))
        .and(header("Authorization", "Bearer admin-special"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "success": true,
            "data": { "id": 7, "username": "ops", "role": 100, "status": 1 }
        })))
        .mount(&server)
        .await;
    let client = NewapiClient::new(server.uri(), "user-x", Some("admin-special".into())).unwrap();
    let u = client.get_user_self().await.unwrap();
    assert_eq!(u.username, "ops");
}

#[tokio::test]
async fn get_user_self_falls_back_to_user_token_when_no_admin() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/api/user/self"))
        .and(header("Authorization", "Bearer just-user"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "success": true,
            "data": { "id": 7, "username": "ops", "role": 1, "status": 1 }
        })))
        .mount(&server)
        .await;
    let client = NewapiClient::new(server.uri(), "just-user", None).unwrap();
    let u = client.get_user_self().await.unwrap();
    assert_eq!(u.username, "ops");
}
