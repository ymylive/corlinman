//! OneBot v11 forward-WebSocket client.
//!
//! corlinman is the **client** — it dials out to gocq / Lagrange / NapCatQQ —
//! matching the "forward WebSocket" mode described in the OneBot v11 spec
//! (<https://github.com/botuniverse/onebot-11>). No reverse-WS listener,
//! no HTTP POST mode.
//!
//! Connection URL: `ws://host:port/` (+ optional `Authorization: Bearer <token>`
//! header if `access_token` is configured).
//!
//! The `run` loop reconnects on failure with a capped backoff of
//! `1s → 2s → 5s → 10s → 30s` (then saturates at 30s). A [`CancellationToken`]
//! lets the caller shut the client down cleanly.
//!
//! # Channel topology
//!
//! ```text
//! gocq/NapCat  <── WS ──>  OneBotClient
//!                            │   ▲
//!                    event_tx│   │action_rx
//!                            ▼   │
//!                    router + command dispatcher
//! ```
//!
//! Tests (see `tests/onebot_integration.rs`) spin up an in-process mock server
//! using `tokio-tungstenite::accept_async` and assert the wire shapes.

use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use http::Request;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::protocol::Message;
use tokio_tungstenite::{connect_async, tungstenite::client::IntoClientRequest};
use tokio_util::sync::CancellationToken;

use super::message::{Action, Event};

/// Backoff schedule in seconds between reconnect attempts (last entry repeats).
pub const RECONNECT_SCHEDULE: &[Duration] = &[
    Duration::from_secs(1),
    Duration::from_secs(2),
    Duration::from_secs(5),
    Duration::from_secs(10),
    Duration::from_secs(30),
];

/// Self-ping interval. Matches the heartbeat cadence NapCat expects on idle
/// connections.
pub const PING_INTERVAL: Duration = Duration::from_secs(30);

/// Configuration for [`OneBotClient`].
#[derive(Debug, Clone)]
pub struct OneBotConfig {
    /// Full WebSocket URL, e.g. `ws://127.0.0.1:3001`.
    pub url: String,
    /// Optional bearer token sent as `Authorization: Bearer <token>`.
    pub access_token: Option<String>,
}

/// Forward-WS client handle.
///
/// The client is move-consumed by [`run`] so the caller cannot accidentally
/// reuse the same inner channels across reconnects.
pub struct OneBotClient {
    cfg: OneBotConfig,
    event_tx: mpsc::Sender<Event>,
    action_rx: mpsc::Receiver<Action>,
    /// Schedule used by [`run`]; overridable in tests to avoid the 1s floor.
    reconnect_schedule: Vec<Duration>,
}

impl OneBotClient {
    pub fn new(
        cfg: OneBotConfig,
        event_tx: mpsc::Sender<Event>,
        action_rx: mpsc::Receiver<Action>,
    ) -> Self {
        Self {
            cfg,
            event_tx,
            action_rx,
            reconnect_schedule: RECONNECT_SCHEDULE.to_vec(),
        }
    }

    /// Override the reconnect schedule. Test-only helper.
    pub fn with_reconnect_schedule(mut self, schedule: Vec<Duration>) -> Self {
        self.reconnect_schedule = schedule;
        self
    }

    /// Main loop: connect → pump events/actions → on disconnect, sleep
    /// `reconnect_schedule[attempt]` (saturating at the last entry) → retry.
    /// Returns once `cancel` fires.
    pub async fn run(mut self, cancel: CancellationToken) -> anyhow::Result<()> {
        let mut attempt: usize = 0;
        loop {
            if cancel.is_cancelled() {
                return Ok(());
            }

            match self.connect_once(&cancel).await {
                Ok(()) => {
                    // Clean disconnect (server closed, or cancel fired). If it
                    // was a cancel, exit; otherwise reset backoff and reconnect.
                    if cancel.is_cancelled() {
                        return Ok(());
                    }
                    attempt = 0;
                }
                Err(e) => {
                    tracing::warn!(target: "corlinman.qq", error = %e, attempt, "onebot ws connection failed");
                }
            }

            let delay = self
                .reconnect_schedule
                .get(attempt)
                .copied()
                .unwrap_or_else(|| {
                    *self
                        .reconnect_schedule
                        .last()
                        .unwrap_or(&Duration::from_secs(30))
                });
            attempt = attempt.saturating_add(1);

            tokio::select! {
                _ = cancel.cancelled() => return Ok(()),
                _ = tokio::time::sleep(delay) => {}
            }
        }
    }

    async fn connect_once(&mut self, cancel: &CancellationToken) -> anyhow::Result<()> {
        let req = build_request(&self.cfg)?;
        tracing::info!(target: "corlinman.qq", url = %self.cfg.url, "onebot ws connecting");
        let (ws, _resp) = connect_async(req).await?;
        let (mut sink, mut stream) = ws.split();

        tracing::info!(target: "corlinman.qq", "onebot ws connected");

        let mut ping = tokio::time::interval(PING_INTERVAL);
        // Skip the immediate tick — we don't want a ping flood at reconnect.
        ping.tick().await;

        loop {
            tokio::select! {
                biased;

                _ = cancel.cancelled() => {
                    let _ = sink.send(Message::Close(None)).await;
                    return Ok(());
                }

                // Outbound: action → JSON → frame
                maybe_action = self.action_rx.recv() => {
                    let Some(action) = maybe_action else {
                        // action channel closed; caller dropped — exit cleanly.
                        let _ = sink.send(Message::Close(None)).await;
                        return Ok(());
                    };
                    let body = serde_json::to_string(&action)?;
                    sink.send(Message::Text(body)).await?;
                }

                // Inbound: frame → JSON → Event
                maybe_msg = stream.next() => {
                    let Some(msg) = maybe_msg else {
                        // peer closed
                        return Ok(());
                    };
                    match msg? {
                        Message::Text(txt) => {
                            match serde_json::from_str::<Event>(&txt) {
                                Ok(ev) => {
                                    // Drop events if the consumer is gone; don't
                                    // block the WS reader.
                                    if self.event_tx.send(ev).await.is_err() {
                                        return Ok(());
                                    }
                                }
                                Err(e) => {
                                    tracing::debug!(target: "corlinman.qq", error = %e, "non-event frame");
                                }
                            }
                        }
                        Message::Binary(_) => { /* OneBot v11 uses text only */ }
                        Message::Ping(p) => { sink.send(Message::Pong(p)).await?; }
                        Message::Pong(_) => { /* keep-alive ack */ }
                        Message::Close(_) => return Ok(()),
                        Message::Frame(_) => {}
                    }
                }

                _ = ping.tick() => {
                    sink.send(Message::Ping(Vec::new())).await?;
                }
            }
        }
    }
}

/// Build an `http::Request` with the optional Bearer token header.
fn build_request(cfg: &OneBotConfig) -> anyhow::Result<Request<()>> {
    let mut req = cfg.url.as_str().into_client_request()?;
    if let Some(token) = &cfg.access_token {
        let value = format!("Bearer {token}")
            .parse()
            .map_err(|e| anyhow::anyhow!("invalid access_token for header: {e}"))?;
        req.headers_mut().insert("Authorization", value);
    }
    Ok(req)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_without_token_has_no_auth_header() {
        let cfg = OneBotConfig {
            url: "ws://127.0.0.1:3001".into(),
            access_token: None,
        };
        let req = build_request(&cfg).unwrap();
        assert!(!req.headers().contains_key("Authorization"));
    }

    #[test]
    fn request_with_token_sets_bearer() {
        let cfg = OneBotConfig {
            url: "ws://127.0.0.1:3001".into(),
            access_token: Some("s3cret".into()),
        };
        let req = build_request(&cfg).unwrap();
        assert_eq!(req.headers().get("Authorization").unwrap(), "Bearer s3cret");
    }

    #[test]
    fn reconnect_schedule_shape() {
        let s = RECONNECT_SCHEDULE;
        assert_eq!(s[0], Duration::from_secs(1));
        assert_eq!(*s.last().unwrap(), Duration::from_secs(30));
    }
}
