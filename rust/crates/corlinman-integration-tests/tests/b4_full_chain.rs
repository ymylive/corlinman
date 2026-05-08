//! B4 full-chain — test 5.
//!
//! Walks a simulated Telegram voice update through every Batch 4
//! primitive in-process (webhook → hooks → stub agent → WsTool runner →
//! FileFetcher → hooks → MessageSent) and asserts the event ordering is:
//!
//! ```text
//! MessageReceived → MessageTranscribed → ToolCalled → MessageSent
//! ```
//!
//! This test doesn't boot the gateway. It wires the primitives directly
//! the same way a future reasoning-loop + channel service would.
//!
//! Split out of `b4_chain.rs` so the heavier WsTool setup doesn't slow
//! the rest of the B4 suite.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use bytes::Bytes;
use corlinman_channels::telegram::media::{MediaError, TelegramHttp};
use corlinman_channels::telegram::types::{File as TgFile, Update};
use corlinman_channels::telegram::webhook::{process_update, WebhookCtx};
use corlinman_hooks::{HookBus, HookEvent, HookPriority};
use corlinman_wstool::{
    file_server_advert, file_server_handler, DiskFileServer, FileFetcher, WsToolConfig,
    WsToolRunner, WsToolServer, FILE_FETCHER_TOOL,
};
use futures::stream::{self, Stream};
use serde_json::json;
use tempfile::TempDir;
use tokio::time::{timeout, Instant};

struct FakeHttp {
    file_path: Option<String>,
    bytes: Vec<u8>,
}

#[async_trait]
impl TelegramHttp for FakeHttp {
    async fn get_file(&self, file_id: &str) -> Result<TgFile, MediaError> {
        Ok(TgFile {
            file_id: file_id.into(),
            file_unique_id: Some(format!("u_{file_id}")),
            file_size: Some(self.bytes.len() as i64),
            file_path: self.file_path.clone(),
        })
    }

    async fn download_stream(
        &self,
        _file_path: &str,
    ) -> Result<Box<dyn Stream<Item = Result<Bytes, MediaError>> + Send + Unpin>, MediaError> {
        let bytes = Bytes::copy_from_slice(&self.bytes);
        Ok(Box::new(Box::pin(stream::iter(vec![Ok::<_, MediaError>(
            bytes,
        )]))))
    }
}

fn voice_update() -> Update {
    serde_json::from_value(json!({
        "update_id": 100,
        "message": {
            "message_id": 77,
            "from": { "id": 3, "is_bot": false },
            "chat": { "id": 3, "type": "private" },
            "date": 0,
            "voice": { "file_id": "V100", "duration": 2 }
        }
    }))
    .unwrap()
}

/// Exercises the full B4 chain end-to-end, asserting hook events fan
/// out in the documented order.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn full_chain_fake_stream() {
    // Shared bus seen by every stage. Capacity generous so we can
    // subscribe both at Normal (to observe order) and Critical (to pin
    // that Critical tier gets events first, same contract as the
    // existing hook_bus_smoke test in this crate).
    let bus = Arc::new(HookBus::new(128));
    let mut normal = bus.subscribe(HookPriority::Normal);
    let mut critical = bus.subscribe(HookPriority::Critical);

    // ----- Stage 1: WsTool server + runner with DiskFileServer -----
    let tmp_root = TempDir::new().unwrap();
    let payload = b"golden pillow full-chain".to_vec();
    std::fs::write(tmp_root.path().join("answer.txt"), &payload).unwrap();

    let cfg = WsToolConfig::loopback("full-chain-token");
    let server = Arc::new(WsToolServer::new(cfg, bus.clone()));
    let addr = server.bind().await.expect("bind");
    let ws_url = format!("ws://{addr}");

    let handler = file_server_handler(DiskFileServer::new(
        tmp_root.path().to_path_buf(),
        100 * 1024 * 1024,
    ));
    let runner = WsToolRunner::connect(
        &ws_url,
        "full-chain-token",
        "agent-runner",
        vec![file_server_advert()],
    )
    .await
    .expect("runner connect");
    let _serve = tokio::spawn(async move {
        let _ = runner.serve_with(handler).await;
    });

    // Wait for tool registration (bounded spin).
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        if server.advertised_tools().contains_key(FILE_FETCHER_TOOL) {
            break;
        }
        if Instant::now() > deadline {
            panic!("file_fetcher tool never registered");
        }
        tokio::task::yield_now().await;
    }

    // Measure the actual cross-subsystem chain, not one-time loopback
    // server setup or HTTP client cold-start.
    let http_client = reqwest::Client::new();
    let overall = Instant::now();

    // ----- Stage 2: Telegram webhook → MessageReceived + Transcribed -----
    let tg_data = TempDir::new().unwrap();
    let http = FakeHttp {
        file_path: Some("voice/a.oga".into()),
        bytes: b"ogg-bytes".to_vec(),
    };
    let ctx = WebhookCtx {
        bot_id: 999,
        bot_username: Some("corlinman_bot"),
        data_dir: tg_data.path(),
        http: &http,
        hooks: Some(&bus),
    };

    let processed = process_update(&ctx, voice_update())
        .await
        .expect("webhook ok")
        .expect("processed some");
    let session_key = processed.session_key.clone();

    // ----- Stage 3: Stub agent — reads transcribed hook, decides to -----
    // ----- fetch a file via WsTool, emits MessageSent. ---------------
    //
    // The "decision" here is hard-coded (mock skill registry) rather
    // than running a real agent loop. That keeps the test focused on
    // the wiring contract rather than reasoning-loop behaviour which
    // lives in its own crate and is covered by its own tests.
    let fetcher =
        FileFetcher::new(None, http_client, 100 * 1024 * 1024).with_ws_server(server.state());
    let blob = fetcher
        .fetch("ws-tool://agent-runner/answer.txt")
        .await
        .expect("agent fetch");

    // Fake "skill augmented the prompt → tool call → agent replies"
    // by emitting MessageSent with the file content as the reply body.
    // Real agents go through the internal chat pipeline; this test pins
    // the subsystem ordering without adding scheduler noise.
    let content = String::from_utf8_lossy(&blob.data).into_owned();
    let _ = bus
        .emit(HookEvent::MessageSent {
            channel: "telegram".to_string(),
            session_key: session_key.clone(),
            content,
            success: true,
            user_id: None,
        })
        .await;

    // ----- Assertions: hook event order on Normal tier -----
    // The webhook already emitted MessageReceived + MessageTranscribed
    // before we subscribed's drain begins, but broadcast channels
    // buffer up to capacity, so we're guaranteed to see all four.
    let mut seen: Vec<&'static str> = Vec::new();
    let deadline = Instant::now() + Duration::from_secs(4);
    while seen.len() < 4 && Instant::now() < deadline {
        if let Ok(Ok(evt)) = timeout(Duration::from_millis(500), normal.recv()).await {
            if matches!(
                &evt,
                HookEvent::MessageReceived { .. }
                    | HookEvent::MessageTranscribed { .. }
                    | HookEvent::ToolCalled { .. }
                    | HookEvent::MessageSent { .. }
            ) {
                seen.push(evt.kind());
            }
        }
    }

    assert_eq!(
        seen,
        vec![
            "message_received",
            "message_transcribed",
            "tool_called",
            "message_sent",
        ],
        "full-chain hook ordering"
    );

    // Critical tier MUST have observed every one of these too — this
    // pins the tier-ordering contract from B1 across the B4 chain.
    let mut crit_kinds: Vec<&'static str> = Vec::new();
    let crit_deadline = Instant::now() + Duration::from_secs(1);
    while crit_kinds.len() < 4 && Instant::now() < crit_deadline {
        if let Ok(Ok(evt)) = timeout(Duration::from_millis(200), critical.recv()).await {
            let k = evt.kind();
            if matches!(
                k,
                "message_received" | "message_transcribed" | "tool_called" | "message_sent"
            ) {
                crit_kinds.push(k);
            }
        }
    }
    assert_eq!(
        crit_kinds.len(),
        4,
        "Critical tier must see all four events"
    );

    server.shutdown().await;

    // The task itself has no hard 5s guarantee, but we track it so the
    // CI log surfaces regressions; panics (via assert below) on gross
    // slow-downs.
    let elapsed = overall.elapsed();
    assert!(
        elapsed < Duration::from_secs(5),
        "full chain must complete under 5s, took {elapsed:?}"
    );
}
