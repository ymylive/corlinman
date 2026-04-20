//! Telegram channel driver: `getUpdates` long-poll → `ChatService` → `sendMessage`.
//!
//! This mirrors the shape of `qq::service::run_qq_channel` so gateway `main.rs`
//! looks symmetric:
//!
//! ```text
//!   Telegram API    ◄── HTTPS  ──►   TelegramLongPollClient
//!                                       │
//!                                (update_rx / reply_tx)
//!                                       ▼
//!                                dispatch loop ── ChatService
//! ```
//!
//! # Why not teloxide
//!
//! teloxide provides a dispatcher / dialogue state machine we don't use. Bare
//! `reqwest` is ~200 lines of long-poll and keeps the dep graph tight. See
//! `telegram/mod.rs` for the rationale.
//!
//! # Graceful shutdown
//!
//! `run_telegram_channel` accepts a [`CancellationToken`]. The long-poll call
//! uses `timeout = 25s`; on cancel we let the in-flight poll time out (<=25s)
//! and then exit. The reply task drains pending outbound messages on cancel.

use std::sync::Arc;
use std::time::Duration;

use corlinman_core::config::TelegramChannelConfig;
use corlinman_core::types::ChatRequest;
use corlinman_gateway_api::{
    ChatService, InternalChatEvent, InternalChatRequest, Message as ApiMessage, Role as ApiRole,
};
use futures::StreamExt;
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;

use crate::telegram::message::{
    binding_from_message, is_mentioning_bot, Message, SendMessageParams, Update, User,
};

/// Long-poll timeout (seconds) passed to `getUpdates`. Telegram recommends
/// 25-50s; we use 25 so cancel→exit latency is bounded at ~25s.
const LONG_POLL_TIMEOUT: u64 = 25;

/// Caller-supplied parameters. Struct-style so signature additions don't churn
/// every call site.
pub struct TelegramParams {
    pub config: TelegramChannelConfig,
    pub chat_service: Arc<dyn ChatService>,
    pub model: String,
}

/// Spawn the Telegram long-poll loop and run until `cancel` fires.
///
/// Returns `Ok(())` on a clean shutdown; surfaces a `config` error when
/// `bot_token` is missing or `enabled = false`.
pub async fn run_telegram_channel(
    params: TelegramParams,
    cancel: CancellationToken,
) -> anyhow::Result<()> {
    let TelegramParams {
        config,
        chat_service,
        model,
    } = params;

    if !config.enabled {
        anyhow::bail!("channels.telegram.enabled = false");
    }
    let token = config
        .bot_token
        .as_ref()
        .ok_or_else(|| anyhow::anyhow!("channels.telegram.bot_token is missing"))?
        .resolve()?;

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(LONG_POLL_TIMEOUT + 5))
        .build()?;
    let http = Arc::new(TelegramHttp {
        client,
        token,
        base: "https://api.telegram.org".into(),
    });

    // Discover the bot's own id + @username; needed for the mention check.
    let me = http.get_me().await?;
    tracing::info!(
        target: "corlinman.channels.telegram",
        bot_id = me.id,
        username = me.username.as_deref().unwrap_or(""),
        "telegram bot authenticated"
    );
    let bot_id = me.id;
    let bot_username = me.username.clone();

    let (reply_tx, mut reply_rx) = mpsc::channel::<SendMessageParams>(64);

    // Outbound reply task: drains sendMessage requests.
    let reply_http = http.clone();
    let reply_cancel = cancel.child_token();
    let reply_handle = tokio::spawn(async move {
        loop {
            tokio::select! {
                biased;
                _ = reply_cancel.cancelled() => break,
                maybe = reply_rx.recv() => {
                    let Some(params) = maybe else { break; };
                    if let Err(err) = reply_http.send_message(&params).await {
                        tracing::warn!(
                            target: "corlinman.channels.telegram",
                            error = %err,
                            chat_id = params.chat_id,
                            "sendMessage failed"
                        );
                    }
                }
            }
        }
    });

    // Inbound loop.
    let mut offset: Option<i64> = None;
    let poll_cancel = cancel.child_token();
    let http_poll = http.clone();
    let config = Arc::new(config);
    let chat_service_loop = chat_service.clone();
    let bot_username_arc: Arc<Option<String>> = Arc::new(bot_username);

    let poll_result: anyhow::Result<()> = loop {
        if poll_cancel.is_cancelled() {
            break Ok(());
        }

        let updates = tokio::select! {
            _ = poll_cancel.cancelled() => break Ok(()),
            r = http_poll.get_updates(offset, LONG_POLL_TIMEOUT) => r,
        };

        match updates {
            Ok(list) => {
                for upd in list {
                    // Advance the offset past this update regardless of whether
                    // we forward it — otherwise we'd loop forever on filtered
                    // messages.
                    offset = Some(upd.update_id + 1);

                    let Some(msg) = upd.message else { continue };
                    let chat_id = msg.chat.id;

                    if !chat_allowed(&config.allowed_chat_ids, chat_id) {
                        tracing::debug!(
                            target: "corlinman.channels.telegram",
                            chat_id, "chat not in allowed_chat_ids; skipping"
                        );
                        continue;
                    }

                    let mentioned = is_bot_addressed(&msg, bot_id, bot_username_arc.as_deref());

                    // Groups: optional @mention gate and keyword filter.
                    if !msg.chat.is_private() {
                        if config.require_mention_in_groups && !mentioned {
                            continue;
                        }
                        if !mentioned && !keyword_match(&config.keyword_filter, &msg) {
                            continue;
                        }
                    }

                    let Some(text) = msg.text.clone() else {
                        continue;
                    };
                    if text.trim().is_empty() {
                        continue;
                    }

                    let binding = binding_from_message(&msg, bot_id);
                    let mut req = ChatRequest::new(binding, text);
                    req.message_id = Some(msg.message_id.to_string());
                    req.timestamp = msg.date;
                    req.mentioned = mentioned;

                    let chat_service = chat_service_loop.clone();
                    let reply_tx = reply_tx.clone();
                    let model = model.clone();
                    let task_cancel = poll_cancel.child_token();
                    tokio::spawn(async move {
                        if let Err(err) =
                            handle_one(chat_service, req, msg, model, reply_tx, task_cancel).await
                        {
                            tracing::warn!(
                                target: "corlinman.channels.telegram",
                                error = %err,
                                "telegram message dispatch failed"
                            );
                        }
                    });
                }
            }
            Err(err) => {
                tracing::warn!(
                    target: "corlinman.channels.telegram",
                    error = %err,
                    "getUpdates failed; backing off 5s"
                );
                tokio::select! {
                    _ = poll_cancel.cancelled() => break Ok(()),
                    _ = tokio::time::sleep(Duration::from_secs(5)) => {}
                }
            }
        }
    };

    // Drop the sender so the reply task's recv returns None.
    drop(reply_tx);
    let _ = reply_handle.await;

    poll_result
}

/// Composite check: entity mention OR reply-to-bot-message. Pulled out so the
/// main loop stays readable.
fn is_bot_addressed(msg: &Message, bot_id: i64, bot_username: Option<&str>) -> bool {
    if is_mentioning_bot(msg, bot_id, bot_username) {
        return true;
    }
    if let Some(reply) = &msg.reply_to_message {
        if let Some(from) = &reply.from {
            if from.id == bot_id && from.is_bot {
                return true;
            }
        }
    }
    false
}

/// Whitelist check: empty list = dispatch-all.
fn chat_allowed(allow: &[i64], chat_id: i64) -> bool {
    allow.is_empty() || allow.contains(&chat_id)
}

/// Case-insensitive substring match over [`TelegramChannelConfig::keyword_filter`].
/// Empty filter list = dispatch-all (match).
fn keyword_match(filter: &[String], msg: &Message) -> bool {
    if filter.is_empty() {
        return true;
    }
    let Some(text) = msg.text.as_deref() else {
        return false;
    };
    let lower = text.to_lowercase();
    filter.iter().any(|kw| lower.contains(&kw.to_lowercase()))
}

/// Run one chat turn: collect tokens, post reply.
async fn handle_one(
    chat_service: Arc<dyn ChatService>,
    req: ChatRequest,
    src: Message,
    model: String,
    reply_tx: mpsc::Sender<SendMessageParams>,
    cancel: CancellationToken,
) -> anyhow::Result<()> {
    let internal = InternalChatRequest {
        model,
        messages: vec![ApiMessage {
            role: ApiRole::User,
            content: req.content.clone(),
        }],
        session_key: req.session_key.clone(),
        stream: true,
        max_tokens: None,
        temperature: None,
        // T1/T3 added these; Telegram adapter (T4) doesn't parse multimodal
        // segments or backfill transport binding yet — leave empty/None so
        // the workspace compiles without touching T4's routing logic.
        attachments: Vec::new(),
        binding: Some(req.binding.clone()),
    };

    let mut stream = chat_service.run(internal, cancel).await;
    let mut text = String::new();
    let mut had_error: Option<String> = None;
    while let Some(ev) = stream.next().await {
        match ev {
            InternalChatEvent::TokenDelta(t) => text.push_str(&t),
            InternalChatEvent::ToolCall { .. } => {}
            InternalChatEvent::Done { .. } => break,
            InternalChatEvent::Error(e) => {
                had_error = Some(e.message);
                break;
            }
        }
    }

    let body = if let Some(err) = had_error {
        format!("[corlinman error] {err}")
    } else if text.trim().is_empty() {
        return Ok(());
    } else {
        text
    };

    let params = SendMessageParams {
        chat_id: src.chat.id,
        text: body,
        // Private chats: no reply-to needed. Groups: reply anchors the bot's
        // answer to the user's message so threading is readable.
        reply_to_message_id: if src.chat.is_private() {
            None
        } else {
            Some(src.message_id)
        },
    };
    reply_tx
        .send(params)
        .await
        .map_err(|e| anyhow::anyhow!("reply channel closed: {e}"))?;
    Ok(())
}

// ============================================================================
// HTTP client
// ============================================================================

/// Thin wrapper around `reqwest::Client` + bot token. Kept private — callers
/// use [`run_telegram_channel`] at the crate boundary.
struct TelegramHttp {
    client: reqwest::Client,
    token: String,
    /// `https://api.telegram.org`. Overridable for tests but we don't need to
    /// go that far today.
    base: String,
}

impl TelegramHttp {
    fn endpoint(&self, method: &str) -> String {
        format!("{}/bot{}/{}", self.base, self.token, method)
    }

    async fn get_me(&self) -> anyhow::Result<User> {
        let resp = self
            .client
            .get(self.endpoint("getMe"))
            .send()
            .await?
            .error_for_status()?;
        let env: TgEnvelope<User> = resp.json().await?;
        env.into_result()
    }

    async fn get_updates(
        &self,
        offset: Option<i64>,
        timeout_secs: u64,
    ) -> anyhow::Result<Vec<Update>> {
        let mut query: Vec<(&str, String)> = vec![("timeout", timeout_secs.to_string())];
        if let Some(off) = offset {
            query.push(("offset", off.to_string()));
        }
        let resp = self
            .client
            .get(self.endpoint("getUpdates"))
            .query(&query)
            .send()
            .await?
            .error_for_status()?;
        let env: TgEnvelope<Vec<Update>> = resp.json().await?;
        env.into_result()
    }

    async fn send_message(&self, params: &SendMessageParams) -> anyhow::Result<()> {
        let resp = self
            .client
            .post(self.endpoint("sendMessage"))
            .json(params)
            .send()
            .await?;
        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            anyhow::bail!("sendMessage {status}: {body}");
        }
        Ok(())
    }
}

/// Telegram wraps every response in `{ok, result, description?}`.
#[derive(serde::Deserialize)]
struct TgEnvelope<T> {
    ok: bool,
    #[serde(default)]
    description: Option<String>,
    #[serde(default = "Option::default")]
    result: Option<T>,
}

impl<T> TgEnvelope<T> {
    fn into_result(self) -> anyhow::Result<T> {
        if !self.ok {
            anyhow::bail!(
                "telegram api error: {}",
                self.description.unwrap_or_default()
            );
        }
        self.result
            .ok_or_else(|| anyhow::anyhow!("telegram api returned ok=true but no result"))
    }
}

// ============================================================================
// Tests
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;

    fn group_msg(text: &str) -> Message {
        serde_json::from_value(serde_json::json!({
            "message_id": 1,
            "from": { "id": 77, "is_bot": false },
            "chat": { "id": -100, "type": "supergroup" },
            "date": 0,
            "text": text,
        }))
        .unwrap()
    }

    fn private_msg(text: &str) -> Message {
        serde_json::from_value(serde_json::json!({
            "message_id": 1,
            "from": { "id": 77, "is_bot": false },
            "chat": { "id": 77, "type": "private" },
            "date": 0,
            "text": text,
        }))
        .unwrap()
    }

    #[test]
    fn filter_keyword_skips_unrelated() {
        // filter contains "bot"; "hello" does not contain it → skip.
        let filter = vec!["bot".to_string()];
        let m = group_msg("hello");
        assert!(!keyword_match(&filter, &m));
        let m2 = group_msg("hey Bot are you there");
        assert!(keyword_match(&filter, &m2));
    }

    #[test]
    fn empty_keyword_filter_matches_all() {
        let filter: Vec<String> = vec![];
        assert!(keyword_match(&filter, &group_msg("anything")));
    }

    #[test]
    fn whitelist_chat_id_rejects_others() {
        let allow: Vec<i64> = vec![-100];
        assert!(chat_allowed(&allow, -100));
        assert!(!chat_allowed(&allow, -200));
    }

    #[test]
    fn empty_whitelist_allows_every_chat() {
        let allow: Vec<i64> = vec![];
        assert!(chat_allowed(&allow, -100));
        assert!(chat_allowed(&allow, 12345));
    }

    #[test]
    fn reply_to_bot_counts_as_addressed() {
        let raw = serde_json::json!({
            "message_id": 2,
            "from": { "id": 77, "is_bot": false },
            "chat": { "id": -100, "type": "supergroup" },
            "date": 0,
            "text": "yes please",
            "reply_to_message": {
                "message_id": 1,
                "from": { "id": 999, "is_bot": true, "username": "corlinman_bot" },
                "chat": { "id": -100, "type": "supergroup" },
                "date": 0,
                "text": "Need anything?"
            }
        });
        let m: Message = serde_json::from_value(raw).unwrap();
        assert!(is_bot_addressed(&m, 999, Some("corlinman_bot")));
    }

    #[test]
    fn reply_to_other_user_is_not_mention() {
        let raw = serde_json::json!({
            "message_id": 2,
            "from": { "id": 77, "is_bot": false },
            "chat": { "id": -100, "type": "supergroup" },
            "date": 0,
            "text": "agree",
            "reply_to_message": {
                "message_id": 1,
                "from": { "id": 42, "is_bot": false },
                "chat": { "id": -100, "type": "supergroup" },
                "date": 0,
                "text": "something"
            }
        });
        let m: Message = serde_json::from_value(raw).unwrap();
        assert!(!is_bot_addressed(&m, 999, Some("corlinman_bot")));
    }

    #[test]
    fn bot_id_used_when_no_username() {
        // text_mention variant — purely id-based, bot may not have a username.
        let raw = serde_json::json!({
            "message_id": 1,
            "from": { "id": 77, "is_bot": false },
            "chat": { "id": -100, "type": "group" },
            "date": 0,
            "text": "hi bot",
            "entities": [
                { "type": "text_mention", "offset": 3, "length": 3,
                  "user": { "id": 999, "is_bot": true } }
            ]
        });
        let m: Message = serde_json::from_value(raw).unwrap();
        assert!(is_bot_addressed(&m, 999, None));
    }

    #[test]
    fn private_messages_bypass_keyword_filter() {
        // Private chats don't apply keyword / mention gates — verified at the
        // call site (main loop); here we just confirm the helpers stay
        // coherent when chat.is_private.
        let m = private_msg("random");
        assert!(m.chat.is_private());
    }
}
