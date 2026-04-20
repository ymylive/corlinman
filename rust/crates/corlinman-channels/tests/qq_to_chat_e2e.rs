//! End-to-end integration test: mock gocq WS → `run_qq_channel` → fake
//! `ChatService` → `send_group_msg` action back to mock gocq.
//!
//! The mock gocq server uses `tokio-tungstenite::accept_async` just like the
//! lower-level `onebot_integration.rs` test — no real network, no real QQ.
//! The fake `ChatService` yields a canned "Hello <user>" streaming response
//! so we can assert the action the channel eventually emits.

use std::time::Duration;

use async_trait::async_trait;
use corlinman_channels::service::{run_qq_channel, QqChannelParams};
use corlinman_core::config::QqChannelConfig;
use corlinman_gateway_api::{ChatEventStream, ChatService, InternalChatEvent, InternalChatRequest};
use futures::stream;
use futures_util::{SinkExt, StreamExt};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::oneshot;
use tokio_tungstenite::tungstenite::Message as WsMessage;
use tokio_tungstenite::WebSocketStream;
use tokio_util::sync::CancellationToken;

/// Fake chat service: emits "Hello <first-user-message>" as two token deltas,
/// then Done. Enough to prove token streaming and reassembly work.
struct FakeChatService;

#[async_trait]
impl ChatService for FakeChatService {
    async fn run(&self, req: InternalChatRequest, _cancel: CancellationToken) -> ChatEventStream {
        let user_text = req
            .messages
            .iter()
            .map(|m| m.content.as_str())
            .next()
            .unwrap_or("")
            .to_string();
        let events = vec![
            InternalChatEvent::TokenDelta("Hello ".into()),
            InternalChatEvent::TokenDelta(user_text),
            InternalChatEvent::Done {
                finish_reason: "stop".into(),
                usage: None,
            },
        ];
        Box::pin(stream::iter(events))
    }
}

async fn accept_one(listener: &TcpListener) -> WebSocketStream<TcpStream> {
    let (stream, _) = listener.accept().await.expect("tcp accept");
    tokio_tungstenite::accept_async(stream)
        .await
        .expect("ws handshake")
}

fn sample_group_event_json(group_id: i64, self_id: i64, text: &str) -> String {
    serde_json::json!({
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "time": 1_700_000_000,
        "self_id": self_id,
        "user_id": 555,
        "group_id": group_id,
        "message_id": 42,
        "message": [
            { "type": "text", "data": { "text": text } }
        ],
        "raw_message": text,
        "sender": { "user_id": 555, "nickname": "tester" }
    })
    .to_string()
}

#[tokio::test]
async fn qq_message_triggers_chat_and_replies_via_send_group_msg() {
    // 1. Start mock gocq WS server.
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let url = format!("ws://{addr}");

    // Mock gocq: accept one connection, push one group message, wait for the
    // send_group_msg action, then close.
    let (captured_tx, captured_rx) = oneshot::channel::<serde_json::Value>();
    let server = tokio::spawn(async move {
        let mut ws = accept_one(&listener).await;

        // Push an inbound group message that matches the configured keyword.
        let payload = sample_group_event_json(12345, 100, "格兰早上好");
        ws.send(WsMessage::Text(payload)).await.unwrap();

        // Wait for the reply action.
        loop {
            let frame = tokio::time::timeout(Duration::from_secs(5), ws.next())
                .await
                .expect("reply action arrives")
                .expect("stream open")
                .expect("no ws error");
            if let WsMessage::Text(t) = frame {
                let v: serde_json::Value = serde_json::from_str(&t).expect("valid json");
                if v["action"].as_str() == Some("send_group_msg") {
                    let _ = captured_tx.send(v);
                    break;
                }
            }
        }
        let _ = ws.close(None).await;
    });

    // 2. Build channel params pointing at the mock server.
    let mut group_keywords = HashMap::new();
    group_keywords.insert("12345".to_string(), vec!["格兰".to_string()]);
    let cfg = QqChannelConfig {
        enabled: true,
        ws_url: url,
        access_token: None,
        self_ids: vec![100],
        group_keywords,
        rate_limit: Default::default(),
    };

    let chat_service: Arc<dyn ChatService> = Arc::new(FakeChatService);
    let params = QqChannelParams {
        config: cfg,
        model: "fake-model".into(),
        chat_service,
        rate_limit_hook: None,
    };

    let cancel = CancellationToken::new();
    let channel_cancel = cancel.clone();
    let channel_handle = tokio::spawn(async move { run_qq_channel(params, channel_cancel).await });

    // 3. Wait for the action the server captured.
    let action = tokio::time::timeout(Duration::from_secs(10), captured_rx)
        .await
        .expect("action captured before timeout")
        .expect("oneshot delivered");

    assert_eq!(action["action"], "send_group_msg");
    assert_eq!(action["params"]["group_id"], 12345);
    let msg_arr = action["params"]["message"]
        .as_array()
        .expect("message array");
    // First segment: @user, Second segment: text containing "Hello ..."
    assert_eq!(msg_arr[0]["type"], "at");
    assert_eq!(msg_arr[0]["data"]["qq"], "555");
    assert_eq!(msg_arr[1]["type"], "text");
    let reply_text = msg_arr[1]["data"]["text"].as_str().unwrap();
    assert!(
        reply_text.contains("Hello"),
        "reply missing Hello: {reply_text}"
    );
    assert!(
        reply_text.contains("格兰早上好"),
        "reply missing echoed user text: {reply_text}"
    );

    // 4. Clean shutdown.
    cancel.cancel();
    let _ = tokio::time::timeout(Duration::from_secs(5), channel_handle).await;
    let _ = tokio::time::timeout(Duration::from_secs(2), server).await;
}
