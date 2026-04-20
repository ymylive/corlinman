//! Channel dispatcher: `MessageEvent` → keyword filter → rate-limit →
//! `ChatRequest`.
//!
//! The router is the first point inside corlinman that has an opinion about
//! *whether* to respond. It reads:
//! - `QQ_GROUP_KEYWORDS` (JSON map) to decide which group messages qualify,
//! - the bot's `self_id` list so `@mention` triggers bypass keyword filtering,
//! - optional per-group / per-sender token buckets
//!   (see [`crate::rate_limit::TokenBucket`]) so runaway keyword hits don't
//!   blast the backend.
//!
//! and emits a transport-agnostic [`ChatRequest`] keyed by `session_key`.
//!
//! The full gateway chat pipeline lives in `corlinman-gateway`; for M5 we stop
//! at emitting the `ChatRequest` so this milestone is testable without spinning
//! up the whole stack.

use std::collections::HashMap;
use std::sync::Arc;

use corlinman_core::channel_binding::ChannelBinding;
use corlinman_core::types::ChatRequest;

use crate::qq::message::{is_mentioned, segments_to_text, MessageEvent, MessageType};
use crate::rate_limit::TokenBucket;

/// Callback invoked by the router whenever a message is silently dropped by
/// a rate-limit check. The gateway wires this to a Prometheus CounterVec
/// (`corlinman_channels_rate_limited_total{channel, reason}`); tests pass a
/// closure that tallies calls in an `Arc<AtomicUsize>`.
///
/// The callback MUST be cheap — it runs on the hot path inline with
/// `dispatch`. Two positional labels: `(channel, reason)`.
pub type RateLimitHook = Arc<dyn Fn(&str, &str) + Send + Sync>;

/// JSON schema for `QQ_GROUP_KEYWORDS`:
///
/// ```json
/// { "123456": ["格兰", "虎子"], "789012": ["爱弥斯"] }
/// ```
///
/// Group ids are stringified because JSON object keys must be strings; values
/// are case-insensitive substring matches against the flattened message text.
///
/// Groups absent from the map default to "dispatch every message" (matches
/// qqBot.js behaviour when `groupKeywordsMap[gid]` is undefined and the bot
/// has no global keyword list configured).
pub type GroupKeywords = HashMap<String, Vec<String>>;

/// Parse `QQ_GROUP_KEYWORDS` env var (JSON). Missing / empty env returns an
/// empty map — dispatch-all for every group.
pub fn parse_group_keywords(raw: &str) -> Result<GroupKeywords, serde_json::Error> {
    if raw.trim().is_empty() {
        return Ok(GroupKeywords::new());
    }
    serde_json::from_str(raw)
}

/// Router state. Cheap to clone (keeps an `Arc` internally once we wire a real
/// config store). For now it just owns the keyword map so tests can construct
/// one in-process.
#[derive(Clone, Default)]
pub struct ChannelRouter {
    /// Per-group keyword filter.
    pub group_keywords: GroupKeywords,
    /// `@mention` targets that always trigger, independent of keywords. In
    /// OneBot this is the bot's own `self_id`, but we leave it as a `Vec` in
    /// case multiple bot accounts share one gateway.
    pub self_ids: Vec<i64>,
    /// Optional per-group token bucket. `None` ⇒ dimension disabled.
    /// Keyed by `"<channel>:<thread>"` (e.g. `"qq:123456"`).
    pub group_limiter: Option<Arc<TokenBucket>>,
    /// Optional per-sender token bucket (scoped by `(channel, thread, sender)`).
    /// Keyed by `"<channel>:<thread>:<sender>"`.
    pub sender_limiter: Option<Arc<TokenBucket>>,
    /// Observation hook fired on every silent drop due to a rate-limit check.
    /// Wired to Prometheus in production; `None` in tests that don't assert
    /// on it.
    pub rate_limit_hook: Option<RateLimitHook>,
}

impl std::fmt::Debug for ChannelRouter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ChannelRouter")
            .field("group_keywords", &self.group_keywords)
            .field("self_ids", &self.self_ids)
            .field("group_limiter", &self.group_limiter.is_some())
            .field("sender_limiter", &self.sender_limiter.is_some())
            .field("rate_limit_hook", &self.rate_limit_hook.is_some())
            .finish()
    }
}

impl ChannelRouter {
    pub fn new(group_keywords: GroupKeywords, self_ids: Vec<i64>) -> Self {
        Self {
            group_keywords,
            self_ids,
            group_limiter: None,
            sender_limiter: None,
            rate_limit_hook: None,
        }
    }

    /// Builder: attach per-group and per-sender token buckets. Either
    /// argument may be `None` to leave that dimension disabled.
    pub fn with_rate_limits(
        mut self,
        group: Option<Arc<TokenBucket>>,
        sender: Option<Arc<TokenBucket>>,
    ) -> Self {
        self.group_limiter = group;
        self.sender_limiter = sender;
        self
    }

    /// Builder: attach a drop-observation hook (typically a Prometheus
    /// counter increment).
    pub fn with_rate_limit_hook(mut self, hook: RateLimitHook) -> Self {
        self.rate_limit_hook = Some(hook);
        self
    }

    /// Apply the keyword/mention gate and return a [`ChatRequest`] if the
    /// message should be forwarded to the chat pipeline.
    ///
    /// Returns `None` when the message is filtered out (heartbeat, wrong
    /// message_type, keyword mismatch, empty body, rate-limited, ...). All
    /// drops are silent — callers log at `debug` if they want visibility, and
    /// rate-limit drops additionally fire [`Self::rate_limit_hook`].
    pub fn dispatch(&self, event: &MessageEvent) -> Option<ChatRequest> {
        let text = flatten_and_trim(&event.message, &event.raw_message);

        // @mention short-circuits keyword filtering. Matches qqBot.js line 298-336.
        let mentioned = self
            .self_ids
            .iter()
            .any(|sid| is_mentioned(&event.message, *sid));

        let binding = match event.message_type {
            MessageType::Private => ChannelBinding::qq_private(event.self_id, event.user_id),
            MessageType::Group => {
                let group_id = event.group_id?;
                if !mentioned && !self.keyword_match(group_id, &text) {
                    return None;
                }
                ChannelBinding::qq_group(event.self_id, group_id, event.user_id)
            }
        };

        // Drop completely empty messages (pure sticker / pure recall placeholder).
        if text.trim().is_empty() {
            return None;
        }

        // Rate-limit gates run AFTER keyword/mention so that dropped messages
        // never consume tokens. Per-group first (cheaper, smaller cardinality).
        if let Some(limiter) = &self.group_limiter {
            let key = format!("{}:{}", binding.channel, binding.thread);
            if !limiter.check(&key) {
                self.fire_hook(&binding.channel, "group");
                return None;
            }
        }
        if let Some(limiter) = &self.sender_limiter {
            let key = format!("{}:{}:{}", binding.channel, binding.thread, binding.sender);
            if !limiter.check(&key) {
                self.fire_hook(&binding.channel, "sender");
                return None;
            }
        }

        let mut req = ChatRequest::new(binding, text);
        req.message_id = Some(event.message_id.to_string());
        req.timestamp = event.time;
        req.mentioned = mentioned;
        Some(req)
    }

    fn fire_hook(&self, channel: &str, reason: &str) {
        if let Some(h) = &self.rate_limit_hook {
            h(channel, reason);
        }
    }

    fn keyword_match(&self, group_id: i64, text: &str) -> bool {
        let Some(kws) = self.group_keywords.get(&group_id.to_string()) else {
            // No keyword list configured → dispatch-all (default).
            return true;
        };
        if kws.is_empty() {
            return true;
        }
        let lower = text.to_lowercase();
        kws.iter().any(|kw| lower.contains(&kw.to_lowercase()))
    }
}

/// Prefer the OneBot-supplied `raw_message` (already CQ-flattened) when
/// present; otherwise fall back to re-extracting from segments.
fn flatten_and_trim(segs: &[crate::qq::message::MessageSegment], raw: &str) -> String {
    if !raw.is_empty() {
        raw.to_string()
    } else {
        segments_to_text(segs)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::qq::message::{MessageSegment, Sender};

    fn group_event(raw: &str, segs: Vec<MessageSegment>, gid: i64) -> MessageEvent {
        MessageEvent {
            self_id: 100,
            message_type: MessageType::Group,
            sub_type: Some("normal".into()),
            group_id: Some(gid),
            user_id: 200,
            message_id: 1,
            message: segs,
            raw_message: raw.into(),
            time: 1700000000,
            sender: Some(Sender::default()),
        }
    }

    #[test]
    fn dispatch_all_when_group_absent_from_map() {
        let router = ChannelRouter::new(GroupKeywords::new(), vec![100]);
        let ev = group_event("随便聊聊", vec![MessageSegment::text("随便聊聊")], 9999);
        let req = router.dispatch(&ev).unwrap();
        assert_eq!(req.content, "随便聊聊");
        assert_eq!(req.binding.thread, "9999");
    }

    #[test]
    fn keyword_match_is_case_insensitive() {
        let mut map = GroupKeywords::new();
        map.insert("123".into(), vec!["格兰".into(), "Aemeath".into()]);
        let router = ChannelRouter::new(map, vec![100]);

        let ev = group_event("hey AEMEATH are you there", vec![], 123);
        assert!(router.dispatch(&ev).is_some());

        let ev2 = group_event("irrelevant chatter", vec![], 123);
        assert!(router.dispatch(&ev2).is_none());
    }

    #[test]
    fn mention_bypasses_keyword_filter() {
        let mut map = GroupKeywords::new();
        map.insert("123".into(), vec!["never_matches".into()]);
        let router = ChannelRouter::new(map, vec![100]);

        let ev = group_event(
            "[CQ:at,qq=100] help",
            vec![MessageSegment::at("100"), MessageSegment::text(" help")],
            123,
        );
        let req = router.dispatch(&ev).unwrap();
        assert!(req.mentioned);
    }

    #[test]
    fn private_message_always_dispatches() {
        let router = ChannelRouter::new(GroupKeywords::new(), vec![100]);
        let ev = MessageEvent {
            self_id: 100,
            message_type: MessageType::Private,
            sub_type: None,
            group_id: None,
            user_id: 77,
            message_id: 1,
            message: vec![MessageSegment::text("hi")],
            raw_message: "hi".into(),
            time: 1,
            sender: None,
        };
        let req = router.dispatch(&ev).unwrap();
        assert_eq!(req.binding.channel, "qq");
        assert_eq!(req.binding.thread, "77");
    }

    #[test]
    fn empty_group_message_drops() {
        let router = ChannelRouter::new(GroupKeywords::new(), vec![100]);
        let ev = group_event("", vec![], 123);
        assert!(router.dispatch(&ev).is_none());
    }

    #[test]
    fn parse_keywords_env_json() {
        let raw = r#"{"123":["a","b"],"456":["c"]}"#;
        let m = parse_group_keywords(raw).unwrap();
        assert_eq!(m.get("123").unwrap().len(), 2);
    }

    #[test]
    fn parse_empty_env_returns_empty_map() {
        assert!(parse_group_keywords("").unwrap().is_empty());
        assert!(parse_group_keywords("   ").unwrap().is_empty());
    }

    #[test]
    fn session_key_stable_across_events() {
        let router = ChannelRouter::new(GroupKeywords::new(), vec![100]);
        let ev1 = group_event("一号消息", vec![MessageSegment::text("一号消息")], 321);
        let ev2 = group_event("二号消息", vec![MessageSegment::text("二号消息")], 321);
        let r1 = router.dispatch(&ev1).unwrap();
        let r2 = router.dispatch(&ev2).unwrap();
        assert_eq!(r1.session_key, r2.session_key);
    }

    // ------------------------------------------------------------------
    // Rate-limit integration
    // ------------------------------------------------------------------

    use std::sync::atomic::{AtomicUsize, Ordering};

    fn count_hook() -> (RateLimitHook, Arc<AtomicUsize>) {
        let counter = Arc::new(AtomicUsize::new(0));
        let c = counter.clone();
        let hook: RateLimitHook = Arc::new(move |_ch: &str, _reason: &str| {
            c.fetch_add(1, Ordering::Relaxed);
        });
        (hook, counter)
    }

    #[test]
    fn dispatch_drops_when_group_over_limit() {
        let group_bucket = Arc::new(TokenBucket::per_minute(1));
        let (hook, count) = count_hook();
        let router = ChannelRouter::new(GroupKeywords::new(), vec![100])
            .with_rate_limits(Some(group_bucket), None)
            .with_rate_limit_hook(hook);

        let ev1 = group_event("msg1", vec![MessageSegment::text("msg1")], 555);
        let ev2 = group_event("msg2", vec![MessageSegment::text("msg2")], 555);
        assert!(router.dispatch(&ev1).is_some(), "first msg passes");
        assert!(router.dispatch(&ev2).is_none(), "second msg dropped");
        assert_eq!(count.load(Ordering::Relaxed), 1, "hook fired once");
    }

    #[test]
    fn dispatch_drops_when_sender_over_limit() {
        // Per-group high, per-sender tight — second msg from same sender drops.
        let sender_bucket = Arc::new(TokenBucket::per_minute(1));
        let (hook, count) = count_hook();
        let router = ChannelRouter::new(GroupKeywords::new(), vec![100])
            .with_rate_limits(None, Some(sender_bucket))
            .with_rate_limit_hook(hook);

        let ev1 = group_event("hi", vec![MessageSegment::text("hi")], 777);
        let ev2 = group_event("hi again", vec![MessageSegment::text("hi again")], 777);
        assert!(router.dispatch(&ev1).is_some());
        assert!(router.dispatch(&ev2).is_none());
        assert_eq!(count.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn rate_limit_drops_do_not_cross_groups() {
        // Different groups → different keys → same-bucket exhaustion on one
        // group doesn't suppress the other.
        let group_bucket = Arc::new(TokenBucket::per_minute(1));
        let router = ChannelRouter::new(GroupKeywords::new(), vec![100])
            .with_rate_limits(Some(group_bucket), None);

        let a1 = group_event("msg", vec![MessageSegment::text("msg")], 1);
        let a2 = group_event("msg", vec![MessageSegment::text("msg")], 1);
        let b1 = group_event("msg", vec![MessageSegment::text("msg")], 2);
        assert!(router.dispatch(&a1).is_some());
        assert!(router.dispatch(&a2).is_none());
        assert!(router.dispatch(&b1).is_some(), "group 2 has its own bucket");
    }
}
