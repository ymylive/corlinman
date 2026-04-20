//! `run_qq_channel` — the long-running task that bridges a live OneBot WS
//! session to the gateway's internal chat pipeline.
//!
//! Flow per inbound event:
//!
//! 1. [`OneBotClient`] delivers a decoded [`Event`].
//! 2. Only `Event::Message` survives the filter.
//! 3. [`ChannelRouter::dispatch`] applies keyword / @mention gating and
//!    produces a [`ChatRequest`].
//! 4. A new task is spawned per accepted message so a slow reasoning loop
//!    doesn't block the next inbound event.
//! 5. That task calls [`ChatService::run`], collects every
//!    `TokenDelta`, and on `Done` posts a `send_group_msg` / `send_private_msg`
//!    action back to the OneBot client via the shared action channel.
//!
//! Error handling is deliberately tolerant — this runs for the life of the
//! process and any transient error in one message must not kill the task for
//! every other group.

use std::collections::HashMap;
use std::sync::Arc;

use corlinman_core::config::QqChannelConfig;
use corlinman_core::types::ChatRequest;
use corlinman_gateway_api::{
    ChatService, InternalChatEvent, InternalChatRequest, Message as ApiMessage, Role as ApiRole,
};
use futures::StreamExt;
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;

use crate::qq::message::{
    segments_to_attachments, Action, Event, MessageEvent, MessageSegment, MessageType,
};
use crate::qq::onebot::{OneBotClient, OneBotConfig};
use crate::rate_limit::TokenBucket;
use crate::router::{ChannelRouter, GroupKeywords, RateLimitHook};

/// Parameters the caller (gateway main) passes in. A simple struct so future
/// additions (model overrides per channel, rate limits) don't churn the
/// signature.
pub struct QqChannelParams {
    pub config: QqChannelConfig,
    /// Default chat model; the inbound message carries no model hint today.
    pub model: String,
    /// Shared chat pipeline.
    pub chat_service: Arc<dyn ChatService>,
    /// Observation hook fired each time a message is dropped by a rate-limit
    /// check. Wired to the gateway's
    /// `corlinman_channels_rate_limited_total{channel, reason}` counter in
    /// production; tests leave it `None`.
    pub rate_limit_hook: Option<RateLimitHook>,
}

/// Spawn the QQ channel loop and run until `cancel` fires. Returns `Ok(())`
/// on a clean shutdown; surfaces a `config` error when the WS URL or self_ids
/// are empty (validated caller-side but defended here too).
pub async fn run_qq_channel(
    params: QqChannelParams,
    cancel: CancellationToken,
) -> anyhow::Result<()> {
    let QqChannelParams {
        config,
        model,
        chat_service,
        rate_limit_hook,
    } = params;

    if config.ws_url.is_empty() {
        anyhow::bail!("channels.qq.ws_url is empty");
    }
    if config.self_ids.is_empty() {
        anyhow::bail!("channels.qq.self_ids is empty");
    }

    let access_token = match config.access_token.as_ref() {
        Some(s) => Some(s.resolve()?),
        None => None,
    };

    // Build token buckets up front; `None` on a field = dimension disabled.
    let group_limiter = config
        .rate_limit
        .group_per_min
        .map(|n| Arc::new(TokenBucket::per_minute(n)));
    let sender_limiter = config
        .rate_limit
        .sender_per_min
        .map(|n| Arc::new(TokenBucket::per_minute(n)));

    // GC the live buckets on a child cancel so we stop sweeping at shutdown.
    let gc_cancel = cancel.child_token();
    let _gc_group = group_limiter
        .as_ref()
        .map(|b| b.clone().start_gc(gc_cancel.clone()));
    let _gc_sender = sender_limiter
        .as_ref()
        .map(|b| b.clone().start_gc(gc_cancel.clone()));

    let mut router = ChannelRouter::new(
        keywords_to_router(&config.group_keywords),
        config.self_ids.clone(),
    )
    .with_rate_limits(group_limiter, sender_limiter);
    if let Some(hook) = rate_limit_hook {
        router = router.with_rate_limit_hook(hook);
    }
    let router = Arc::new(router);

    // Channels the OneBotClient and the dispatch loop share.
    let (event_tx, mut event_rx) = mpsc::channel::<Event>(64);
    let (action_tx, action_rx) = mpsc::channel::<Action>(64);

    let client = OneBotClient::new(
        OneBotConfig {
            url: config.ws_url.clone(),
            access_token,
        },
        event_tx,
        action_rx,
    );

    // Hand the OneBot client its own cancel child so both tasks stop together.
    let client_cancel = cancel.child_token();
    let client_handle = tokio::spawn(async move { client.run(client_cancel).await });

    let dispatch_cancel = cancel.child_token();
    let dispatch_handle = {
        let chat_service = chat_service.clone();
        let router = router.clone();
        let action_tx = action_tx.clone();
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    biased;
                    _ = dispatch_cancel.cancelled() => break,
                    maybe_ev = event_rx.recv() => {
                        let Some(ev) = maybe_ev else { break; };
                        let Event::Message(msg_ev) = ev else { continue; };
                        let Some(req) = router.dispatch(&msg_ev) else { continue; };

                        // Spawn the chat task so the next inbound event isn't
                        // blocked by a slow reasoning loop.
                        let chat_service = chat_service.clone();
                        let action_tx = action_tx.clone();
                        let model = model.clone();
                        let cancel = dispatch_cancel.child_token();
                        tokio::spawn(async move {
                            if let Err(err) =
                                handle_one(chat_service, req, msg_ev, model, action_tx, cancel)
                                    .await
                            {
                                tracing::warn!(
                                    target: "corlinman.channels.qq",
                                    error = %err,
                                    "qq message dispatch failed"
                                );
                            }
                        });
                    }
                }
            }
        })
    };

    // Wait for shutdown (cancel) or either task finishing unexpectedly.
    tokio::select! {
        _ = cancel.cancelled() => {}
        res = client_handle => {
            if let Err(join_err) = res {
                tracing::warn!(
                    target: "corlinman.channels.qq",
                    error = %join_err,
                    "onebot client task panicked"
                );
            }
        }
    }

    dispatch_handle.abort();
    Ok(())
}

/// Convert the TOML-loaded `HashMap<String, Vec<String>>` to the router's
/// type alias. They're structurally identical — this just names the intent.
fn keywords_to_router(m: &HashMap<String, Vec<String>>) -> GroupKeywords {
    m.clone()
}

async fn handle_one(
    chat_service: Arc<dyn ChatService>,
    req: ChatRequest,
    event: MessageEvent,
    model: String,
    action_tx: mpsc::Sender<Action>,
    cancel: CancellationToken,
) -> anyhow::Result<()> {
    let internal = build_internal_request(&req, &event, model);

    let mut stream = chat_service.run(internal, cancel.clone()).await;
    let mut text = String::new();
    let mut had_error: Option<String> = None;
    while let Some(ev) = stream.next().await {
        match ev {
            InternalChatEvent::TokenDelta(t) => text.push_str(&t),
            InternalChatEvent::ToolCall { .. } => {
                // Informational — the gateway handles execution.
            }
            InternalChatEvent::Done { .. } => break,
            InternalChatEvent::Error(e) => {
                had_error = Some(e.message);
                break;
            }
        }
    }

    let body = if let Some(err) = had_error {
        // Surface errors as a short system reply so the user knows something
        // failed; M5 scope doesn't define a prettier UX.
        format!("[corlinman error] {err}")
    } else if text.trim().is_empty() {
        // Nothing to send — silently drop (matches qqBot.js behaviour on empty
        // assistant responses).
        return Ok(());
    } else {
        text
    };

    let action = build_reply_action(&event, &body);
    action_tx
        .send(action)
        .await
        .map_err(|e| anyhow::anyhow!("action channel closed: {e}"))?;
    Ok(())
}

/// Convert a routed [`ChatRequest`] + the original [`MessageEvent`] into the
/// `InternalChatRequest` handed to the chat service.
///
/// Attachments are derived from the raw QQ segments here rather than on the
/// router to keep `ChatRequest` purely text; that way schedulers and other
/// non-multimodal callers don't need to thread empty vectors everywhere.
fn build_internal_request(
    req: &ChatRequest,
    event: &MessageEvent,
    model: String,
) -> InternalChatRequest {
    let attachments = segments_to_attachments(&event.message);
    InternalChatRequest {
        model,
        messages: vec![ApiMessage {
            role: ApiRole::User,
            content: req.content.clone(),
        }],
        session_key: req.session_key.clone(),
        stream: true,
        max_tokens: None,
        temperature: None,
        attachments,
        // Backfill the transport binding so gateway-side consumers
        // (context_assembler, approval scoping, daily-note tagging) can reason
        // about provenance without re-deriving it from the session_key.
        binding: Some(req.binding.clone()),
    }
}

/// Build a `send_group_msg` / `send_private_msg` action carrying a single
/// text segment. Group messages prepend an `@sender` so the reply is clearly
/// addressed (matches qqBot.js behaviour for keyword-triggered responses).
fn build_reply_action(event: &MessageEvent, body: &str) -> Action {
    match event.message_type {
        MessageType::Group => {
            let gid = event.group_id.unwrap_or_default();
            let segments = vec![
                MessageSegment::at(event.user_id.to_string()),
                MessageSegment::text(format!(" {body}")),
            ];
            Action::SendGroupMsg {
                group_id: gid,
                message: segments,
            }
        }
        MessageType::Private => Action::SendPrivateMsg {
            user_id: event.user_id,
            message: vec![MessageSegment::text(body.to_string())],
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::qq::message::{KnownSegment, MessageSegment};
    use corlinman_core::channel_binding::ChannelBinding;
    use corlinman_gateway_api::AttachmentKind;

    fn sample_group_event() -> MessageEvent {
        MessageEvent {
            self_id: 100,
            message_type: MessageType::Group,
            sub_type: Some("normal".into()),
            group_id: Some(12345),
            user_id: 555,
            message_id: 42,
            message: vec![MessageSegment::text("格兰早")],
            raw_message: "格兰早".into(),
            time: 1700000000,
            sender: None,
        }
    }

    #[test]
    fn group_reply_addresses_sender() {
        let ev = sample_group_event();
        let a = build_reply_action(&ev, "hello");
        match a {
            Action::SendGroupMsg { group_id, message } => {
                assert_eq!(group_id, 12345);
                assert_eq!(message.len(), 2);
                // first segment is an At targeting user 555
                match message[0].known() {
                    Some(KnownSegment::At { qq }) => assert_eq!(qq, "555"),
                    other => panic!("expected At, got {other:?}"),
                }
                match message[1].known() {
                    Some(KnownSegment::Text { text }) => assert!(text.contains("hello")),
                    other => panic!("expected Text, got {other:?}"),
                }
            }
            other => panic!("expected SendGroupMsg, got {other:?}"),
        }
    }

    #[test]
    fn dispatch_propagates_attachments() {
        let mut ev = sample_group_event();
        ev.message = vec![
            MessageSegment::text("look at this "),
            MessageSegment::Known(KnownSegment::Image {
                url: "https://cdn/pic.png".into(),
                file: Some("pic.png".into()),
            }),
        ];
        ev.raw_message = "look at this [CQ:image,url=https://cdn/pic.png]".into();

        let binding = ChannelBinding::qq_group(ev.self_id, ev.group_id.unwrap(), ev.user_id);
        let mut req = ChatRequest::new(binding, "look at this".into());
        req.message_id = Some(ev.message_id.to_string());

        let internal = build_internal_request(&req, &ev, "claude-sonnet-4-5".into());

        assert_eq!(internal.attachments.len(), 1);
        assert_eq!(internal.attachments[0].kind, AttachmentKind::Image);
        assert_eq!(
            internal.attachments[0].url.as_deref(),
            Some("https://cdn/pic.png")
        );
        assert_eq!(internal.messages.len(), 1);
        assert_eq!(internal.messages[0].content, "look at this");
    }

    #[test]
    fn dispatch_empty_attachments_when_text_only() {
        let ev = sample_group_event();
        let binding = ChannelBinding::qq_group(ev.self_id, ev.group_id.unwrap(), ev.user_id);
        let req = ChatRequest::new(binding, ev.raw_message.clone());
        let internal = build_internal_request(&req, &ev, "claude-sonnet-4-5".into());
        assert!(internal.attachments.is_empty());
    }

    #[test]
    fn private_reply_omits_at() {
        let mut ev = sample_group_event();
        ev.message_type = MessageType::Private;
        ev.group_id = None;
        let a = build_reply_action(&ev, "hi");
        match a {
            Action::SendPrivateMsg { user_id, message } => {
                assert_eq!(user_id, 555);
                assert_eq!(message.len(), 1);
                match message[0].known() {
                    Some(KnownSegment::Text { text }) => assert_eq!(text, "hi"),
                    other => panic!("expected Text, got {other:?}"),
                }
            }
            other => panic!("expected SendPrivateMsg, got {other:?}"),
        }
    }
}
