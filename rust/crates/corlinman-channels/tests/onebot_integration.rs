//! Integration tests for the OneBot v11 forward-WS client.
//!
//! We spin up an in-process mock gocq/NapCat server using
//! `tokio-tungstenite::accept_async`, let `OneBotClient` dial into it, and
//! verify:
//!
//! 1. `connect_receive_parse` — server pushes a group-message event, client
//!    forwards a decoded [`Event::Message`] on its `event_tx` channel, and the
//!    router produces a [`ChatRequest`] with the expected `session_key`.
//! 2. `send_action_received_by_server` — feeding an [`Action`] into
//!    `action_rx` causes the server to receive the right OneBot envelope
//!    (`{"action":"send_group_msg","params":{...}}`).
//! 3. `reconnect_after_drop` — when the server closes the socket, the client
//!    reconnects within the override backoff window and the next event still
//!    reaches the consumer.
//!
//! **No real gocq / no network** — the mock server binds `127.0.0.1:0` so the
//! OS picks a free port, making this safe for CI.

use std::time::Duration;

use corlinman_channels::qq::message::{Action, Event, MessageSegment};
use corlinman_channels::qq::onebot::{OneBotClient, OneBotConfig};
use corlinman_channels::router::{ChannelRouter, GroupKeywords};
use futures_util::{SinkExt, StreamExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, oneshot};
use tokio_tungstenite::tungstenite::Message as WsMessage;
use tokio_tungstenite::WebSocketStream;
use tokio_util::sync::CancellationToken;

/// Accept one WS handshake on `listener` and return the upgraded stream.
async fn accept_one(listener: &TcpListener) -> WebSocketStream<TcpStream> {
    let (stream, _) = listener.accept().await.expect("tcp accept");
    tokio_tungstenite::accept_async(stream)
        .await
        .expect("ws handshake")
}

/// Build a sample OneBot group-message event as a JSON string.
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

/// Spawn an [`OneBotClient`] pointed at `ws_url` with fast reconnect timing
/// and return the client-side channels plus a cancel token.
fn spawn_client(
    ws_url: String,
) -> (
    mpsc::Receiver<Event>,
    mpsc::Sender<Action>,
    CancellationToken,
    tokio::task::JoinHandle<anyhow::Result<()>>,
) {
    let (event_tx, event_rx) = mpsc::channel::<Event>(16);
    let (action_tx, action_rx) = mpsc::channel::<Action>(16);
    let cancel = CancellationToken::new();

    let client = OneBotClient::new(
        OneBotConfig {
            url: ws_url,
            access_token: None,
        },
        event_tx,
        action_rx,
    )
    // Tight backoff so `reconnect_after_drop` stays fast.
    .with_reconnect_schedule(vec![Duration::from_millis(50)]);

    let cancel_c = cancel.clone();
    let handle = tokio::spawn(async move { client.run(cancel_c).await });
    (event_rx, action_tx, cancel, handle)
}

#[tokio::test]
async fn connect_receive_parse() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let url = format!("ws://{addr}");

    let (mut event_rx, _action_tx, cancel, handle) = spawn_client(url);

    // Mock server: accept one connection, push a group-message event, keep open.
    let (done_tx, done_rx) = oneshot::channel();
    let server = tokio::spawn(async move {
        let mut ws = accept_one(&listener).await;
        let payload = sample_group_event_json(12345, 100, "格兰早上好");
        ws.send(WsMessage::Text(payload)).await.unwrap();
        // Wait for shutdown signal rather than dropping immediately, so the
        // client gets a clean read rather than a hang-up.
        let _ = done_rx.await;
        let _ = ws.close(None).await;
    });

    // Receive and assert.
    let ev = tokio::time::timeout(Duration::from_secs(2), event_rx.recv())
        .await
        .expect("event arrives before timeout")
        .expect("event channel open");

    let Event::Message(m) = ev else {
        panic!("expected Message event, got {ev:?}");
    };
    assert_eq!(m.group_id, Some(12345));
    assert_eq!(m.self_id, 100);

    // Router should keyword-filter through and yield a ChatRequest.
    let mut kws = GroupKeywords::new();
    kws.insert("12345".into(), vec!["格兰".into()]);
    let router = ChannelRouter::new(kws, vec![100]);
    let req = router.dispatch(&m).expect("keyword match → dispatch");
    assert_eq!(req.binding.thread, "12345");
    assert_eq!(req.binding.channel, "qq");
    assert_eq!(req.session_key.len(), 16);

    // Clean up.
    let _ = done_tx.send(());
    cancel.cancel();
    let _ = handle.await;
    let _ = server.await;
}

#[tokio::test]
async fn send_action_received_by_server() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let url = format!("ws://{addr}");

    let (_event_rx, action_tx, cancel, handle) = spawn_client(url);

    // Mock server: accept, wait for one text frame, verify envelope shape.
    let server = tokio::spawn(async move {
        let mut ws = accept_one(&listener).await;
        let frame = tokio::time::timeout(Duration::from_secs(2), ws.next())
            .await
            .expect("frame before timeout")
            .expect("stream open")
            .expect("no ws error");
        let txt = match frame {
            WsMessage::Text(t) => t,
            other => panic!("expected Text, got {other:?}"),
        };
        let v: serde_json::Value = serde_json::from_str(&txt).expect("valid json");
        assert_eq!(v["action"], "send_group_msg");
        assert_eq!(v["params"]["group_id"], 123);
        assert_eq!(v["params"]["message"][0]["type"], "reply");
        assert_eq!(v["params"]["message"][0]["data"]["id"], "42");
        assert_eq!(v["params"]["message"][1]["type"], "text");
        assert_eq!(v["params"]["message"][1]["data"]["text"], "hi");
        let _ = ws.close(None).await;
    });

    // Push an action — client should forward it as a Text frame.
    action_tx
        .send(Action::SendGroupMsg {
            group_id: 123,
            message: vec![MessageSegment::reply("42"), MessageSegment::text("hi")],
        })
        .await
        .expect("action send");

    // Wait for the server to finish its assertions.
    tokio::time::timeout(Duration::from_secs(3), server)
        .await
        .expect("server done")
        .expect("server task ok");

    cancel.cancel();
    let _ = handle.await;
}

#[tokio::test]
async fn reconnect_after_drop() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let url = format!("ws://{addr}");

    let (mut event_rx, _action_tx, cancel, handle) = spawn_client(url);

    // Mock server: accept first connection, drop it; accept second connection,
    // push an event and hold open until signalled.
    let (done_tx, done_rx) = oneshot::channel();
    let server = tokio::spawn(async move {
        // First connection — drop immediately after handshake.
        {
            let mut ws = accept_one(&listener).await;
            let _ = ws.close(None).await;
            drop(ws);
        }

        // Second connection — push a distinguishable event.
        let mut ws = accept_one(&listener).await;
        let payload = sample_group_event_json(999, 100, "second-connection-hello");
        ws.send(WsMessage::Text(payload)).await.unwrap();

        let _ = done_rx.await;
        let _ = ws.close(None).await;
    });

    // The client should reconnect (50ms backoff) and then we see the event.
    let ev = tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            match event_rx.recv().await {
                Some(Event::Message(m)) if m.group_id == Some(999) => return m,
                Some(_) => continue,
                None => panic!("event channel closed before second-connection event"),
            }
        }
    })
    .await
    .expect("event arrives on reconnect");

    assert_eq!(ev.raw_message, "second-connection-hello");

    let _ = done_tx.send(());
    cancel.cancel();
    let _ = handle.await;
    let _ = server.await;
}
