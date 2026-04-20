//! Real `ChatService` implementation — bridges [`corlinman_gateway_api`]
//! callers to the same [`ChatBackend`] that serves `/v1/chat/completions`.
//!
//! The HTTP route and this service share a backend handle so they cannot drift:
//! whatever the Python reasoning loop does for an HTTP request also holds for
//! a QQ message arriving via the channels crate.
//!
//! Scope for M5: surface `TokenDelta` → `InternalChatEvent::TokenDelta`,
//! `ToolCall` → event (informational — the backend still ack's with the
//! placeholder), `Done` → terminal, `Error` → terminal. `AwaitingApproval`
//! and streaming `Usage` frames are skipped (they land with the approval
//! pipeline in M6+).

use std::sync::Arc;

use async_trait::async_trait;
use corlinman_agent_client::tool_callback::{PlaceholderExecutor, ToolExecutor};
use corlinman_core::FailoverReason;
use corlinman_gateway_api::{
    Attachment as ApiAttachment, AttachmentKind as ApiAttachmentKind, ChannelBinding,
    ChatEventStream, ChatService as ChatServiceTrait, InternalChatError, InternalChatEvent,
    InternalChatRequest, Role as ApiRole, Usage as ApiUsage,
};
use corlinman_proto::v1::{
    client_frame, server_frame, Attachment as PbAttachment, AttachmentKind as PbAttachmentKind,
    ChannelBinding as PbChannelBinding, ChatStart, ClientFrame, Message as PbMessage,
    Role as PbRole,
};
use futures::{stream, StreamExt};
use tokio_util::sync::CancellationToken;

use crate::routes::chat::ChatBackend;

/// Gateway-side service that wraps any [`ChatBackend`] so it can be driven
/// from in-process callers via the [`ChatServiceTrait`].
pub struct ChatService {
    backend: Arc<dyn ChatBackend>,
    tool_executor: Arc<dyn ToolExecutor>,
}

impl ChatService {
    /// Bundle a backend with the default placeholder tool executor (matches
    /// the wiring in [`crate::routes::chat::ChatState::new`]).
    pub fn new(backend: Arc<dyn ChatBackend>) -> Self {
        Self {
            backend,
            tool_executor: Arc::new(PlaceholderExecutor),
        }
    }

    /// Customise the tool executor — used by tests; production bundles the
    /// placeholder exec (same as the HTTP route) so tool_calls keep the
    /// Python loop progressing.
    pub fn with_tool_executor(mut self, exec: Arc<dyn ToolExecutor>) -> Self {
        self.tool_executor = exec;
        self
    }
}

#[async_trait]
impl ChatServiceTrait for ChatService {
    async fn run(&self, req: InternalChatRequest, cancel: CancellationToken) -> ChatEventStream {
        let start = build_chat_start(&req);
        let backend = self.backend.clone();
        let executor = self.tool_executor.clone();

        match backend.start(start).await {
            Ok((tx, rx)) => Box::pin(into_event_stream(rx, tx, executor, cancel)),
            Err(err) => {
                // Single terminal error event, then close.
                let ev = InternalChatEvent::Error(InternalChatError::from(err));
                Box::pin(stream::iter(vec![ev]))
            }
        }
    }
}

/// Fold a `ServerFrame` stream into `InternalChatEvent`s. Stops after the
/// first terminal frame (`Done` / `Error`) or when `cancel` fires.
fn into_event_stream(
    rx: crate::routes::chat::BackendRx,
    tx: tokio::sync::mpsc::Sender<ClientFrame>,
    executor: Arc<dyn ToolExecutor>,
    cancel: CancellationToken,
) -> impl futures::Stream<Item = InternalChatEvent> + Send {
    struct State {
        rx: crate::routes::chat::BackendRx,
        tx: tokio::sync::mpsc::Sender<ClientFrame>,
        executor: Arc<dyn ToolExecutor>,
        cancel: CancellationToken,
        done: bool,
    }

    let state = State {
        rx,
        tx,
        executor,
        cancel,
        done: false,
    };

    stream::unfold(state, |mut s| async move {
        if s.done {
            return None;
        }
        loop {
            if s.cancel.is_cancelled() {
                s.done = true;
                return Some((
                    InternalChatEvent::Error(InternalChatError {
                        reason: FailoverReason::Unknown,
                        message: "cancelled".into(),
                    }),
                    s,
                ));
            }

            let next = tokio::select! {
                biased;
                _ = s.cancel.cancelled() => {
                    s.done = true;
                    return Some((
                        InternalChatEvent::Error(InternalChatError {
                            reason: FailoverReason::Unknown,
                            message: "cancelled".into(),
                        }),
                        s,
                    ));
                }
                frame = s.rx.next() => frame,
            };

            match next {
                Some(Ok(frame)) => match frame.kind {
                    Some(server_frame::Kind::Token(t)) => {
                        return Some((InternalChatEvent::TokenDelta(t.text), s));
                    }
                    Some(server_frame::Kind::ToolCall(tc)) => {
                        // Echo the placeholder result so the Python reasoning
                        // loop advances — matches what the HTTP handler does.
                        if let Ok(result) = s.executor.execute(&tc).await {
                            let _ =
                                s.tx.send(ClientFrame {
                                    kind: Some(client_frame::Kind::ToolResult(result)),
                                })
                                .await;
                        }
                        return Some((
                            InternalChatEvent::ToolCall {
                                plugin: tc.plugin,
                                tool: tc.tool,
                                args_json: tc.args_json.into(),
                            },
                            s,
                        ));
                    }
                    Some(server_frame::Kind::Done(d)) => {
                        s.done = true;
                        let usage = d.usage.map(|u| ApiUsage {
                            prompt_tokens: u.prompt_tokens,
                            completion_tokens: u.completion_tokens,
                            total_tokens: u.total_tokens,
                        });
                        return Some((
                            InternalChatEvent::Done {
                                finish_reason: d.finish_reason,
                                usage,
                            },
                            s,
                        ));
                    }
                    Some(server_frame::Kind::Error(e)) => {
                        s.done = true;
                        return Some((
                            InternalChatEvent::Error(InternalChatError {
                                reason: reason_from_proto(e.reason),
                                message: e.message,
                            }),
                            s,
                        ));
                    }
                    Some(server_frame::Kind::Awaiting(_))
                    | Some(server_frame::Kind::Usage(_))
                    | None => {
                        // Not surfaced in M5 — pull the next frame.
                        continue;
                    }
                },
                Some(Err(err)) => {
                    s.done = true;
                    return Some((InternalChatEvent::Error(InternalChatError::from(err)), s));
                }
                None => {
                    // Stream ended without Done — synthesise one so callers
                    // always see a terminal event.
                    s.done = true;
                    return Some((
                        InternalChatEvent::Done {
                            finish_reason: "stop".into(),
                            usage: None,
                        },
                        s,
                    ));
                }
            }
        }
    })
}

fn build_chat_start(req: &InternalChatRequest) -> ChatStart {
    let messages = req
        .messages
        .iter()
        .map(|m| PbMessage {
            role: role_to_proto(m.role) as i32,
            content: m.content.clone(),
            name: String::new(),
            tool_call_id: String::new(),
            content_json: Default::default(),
        })
        .collect();
    let attachments = req.attachments.iter().map(attachment_to_proto).collect();
    let binding = req.binding.as_ref().map(binding_to_proto);
    ChatStart {
        model: req.model.clone(),
        messages,
        tools_json: Vec::new(),
        session_key: req.session_key.clone(),
        binding,
        placeholders: Default::default(),
        temperature: req.temperature.unwrap_or(0.0),
        max_tokens: req.max_tokens.unwrap_or(0),
        stream: req.stream,
        trace: None,
        provider_config_json: Vec::new(),
        attachments,
    }
}

/// Convert the in-process [`ChannelBinding`] to its protobuf twin. The
/// `session_key` field on the proto side is the pre-derived key so the
/// Python agent doesn't need to re-hash.
fn binding_to_proto(b: &ChannelBinding) -> PbChannelBinding {
    PbChannelBinding {
        channel: b.channel.clone(),
        account: b.account.clone(),
        thread: b.thread.clone(),
        sender: b.sender.clone(),
        session_key: b.session_key(),
    }
}

/// Convert the in-process [`ApiAttachment`] to its protobuf twin. The enum
/// mapping is explicit — silently defaulting to `UNSPECIFIED` would drop
/// multimodal inputs without a trace.
fn attachment_to_proto(a: &ApiAttachment) -> PbAttachment {
    let kind = match a.kind {
        ApiAttachmentKind::Image => PbAttachmentKind::Image,
        ApiAttachmentKind::Audio => PbAttachmentKind::Audio,
        ApiAttachmentKind::Video => PbAttachmentKind::Video,
        ApiAttachmentKind::File => PbAttachmentKind::File,
    };
    PbAttachment {
        kind: kind as i32,
        url: a.url.clone().unwrap_or_default(),
        bytes: a.bytes.clone().unwrap_or_default(),
        mime: a.mime.clone().unwrap_or_default(),
        file_name: a.file_name.clone().unwrap_or_default(),
    }
}

fn role_to_proto(role: ApiRole) -> PbRole {
    match role {
        ApiRole::User => PbRole::User,
        ApiRole::Assistant => PbRole::Assistant,
        ApiRole::System => PbRole::System,
        ApiRole::Tool => PbRole::Tool,
    }
}

// Separate converter here because the one in `routes::chat` is private.
fn reason_from_proto(code: i32) -> FailoverReason {
    match code {
        1 => FailoverReason::Billing,
        2 => FailoverReason::RateLimit,
        3 => FailoverReason::Auth,
        4 => FailoverReason::AuthPermanent,
        5 => FailoverReason::Timeout,
        6 => FailoverReason::ModelNotFound,
        7 => FailoverReason::Format,
        8 => FailoverReason::ContextOverflow,
        9 => FailoverReason::Overloaded,
        10 => FailoverReason::Unknown,
        _ => FailoverReason::Unspecified,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::routes::chat::{BackendRx, ChatBackend as ChatBackendTrait};
    use corlinman_core::CorlinmanError;
    use corlinman_gateway_api::Message as ApiMessage;
    use corlinman_proto::v1::{
        Done as PbDone, ErrorInfo, ServerFrame, TokenDelta, ToolCall as PbToolCall,
    };
    use futures::stream;
    use std::pin::Pin;
    use tokio::sync::{mpsc, Mutex};

    /// Minimal scripted backend mirroring the one in routes::chat::mock but
    /// kept local so we don't depend on `#[cfg(test)]` items across modules.
    #[derive(Default)]
    struct ScriptedBackend {
        frames: Mutex<Vec<ServerFrame>>,
    }

    impl ScriptedBackend {
        fn new(frames: Vec<ServerFrame>) -> Self {
            Self {
                frames: Mutex::new(frames),
            }
        }
    }

    #[async_trait]
    impl ChatBackendTrait for ScriptedBackend {
        async fn start(
            &self,
            _start: ChatStart,
        ) -> Result<(mpsc::Sender<ClientFrame>, BackendRx), CorlinmanError> {
            let (tx, _rx) = mpsc::channel::<ClientFrame>(8);
            let frames: Vec<_> = std::mem::take(&mut *self.frames.lock().await)
                .into_iter()
                .map(Ok)
                .collect();
            let s: Pin<Box<_>> = Box::pin(stream::iter(frames));
            Ok((tx, s))
        }
    }

    fn sample_req() -> InternalChatRequest {
        InternalChatRequest {
            model: "m".into(),
            messages: vec![ApiMessage {
                role: ApiRole::User,
                content: "hi".into(),
            }],
            session_key: "abc".into(),
            stream: true,
            max_tokens: None,
            temperature: None,
            attachments: Vec::new(),
            binding: None,
        }
    }

    #[tokio::test]
    async fn tokens_and_done_are_surfaced() {
        let backend = Arc::new(ScriptedBackend::new(vec![
            ServerFrame {
                kind: Some(server_frame::Kind::Token(TokenDelta {
                    text: "hello ".into(),
                    is_reasoning: false,
                    seq: 0,
                })),
            },
            ServerFrame {
                kind: Some(server_frame::Kind::Token(TokenDelta {
                    text: "world".into(),
                    is_reasoning: false,
                    seq: 1,
                })),
            },
            ServerFrame {
                kind: Some(server_frame::Kind::Done(PbDone {
                    finish_reason: "stop".into(),
                    usage: None,
                    total_tokens_seen: 0,
                    wall_time_ms: 0,
                })),
            },
        ]));
        let svc = ChatService::new(backend);
        let cancel = CancellationToken::new();
        let mut s = svc.run(sample_req(), cancel).await;
        let mut text = String::new();
        let mut saw_done = false;
        while let Some(ev) = s.next().await {
            match ev {
                InternalChatEvent::TokenDelta(t) => text.push_str(&t),
                InternalChatEvent::Done { finish_reason, .. } => {
                    assert_eq!(finish_reason, "stop");
                    saw_done = true;
                }
                other => panic!("unexpected event: {other:?}"),
            }
        }
        assert_eq!(text, "hello world");
        assert!(saw_done);
    }

    #[tokio::test]
    async fn tool_call_is_surfaced_and_acked() {
        let backend = Arc::new(ScriptedBackend::new(vec![
            ServerFrame {
                kind: Some(server_frame::Kind::ToolCall(PbToolCall {
                    call_id: "c1".into(),
                    plugin: "p".into(),
                    tool: "t".into(),
                    args_json: b"{}".to_vec(),
                    seq: 0,
                })),
            },
            ServerFrame {
                kind: Some(server_frame::Kind::Done(PbDone {
                    finish_reason: "tool_calls".into(),
                    usage: None,
                    total_tokens_seen: 0,
                    wall_time_ms: 0,
                })),
            },
        ]));
        let svc = ChatService::new(backend);
        let mut s = svc.run(sample_req(), CancellationToken::new()).await;
        let mut saw_tool = false;
        while let Some(ev) = s.next().await {
            if let InternalChatEvent::ToolCall { plugin, tool, .. } = ev {
                assert_eq!(plugin, "p");
                assert_eq!(tool, "t");
                saw_tool = true;
            }
        }
        assert!(saw_tool);
    }

    #[tokio::test]
    async fn error_frame_terminates_stream() {
        let backend = Arc::new(ScriptedBackend::new(vec![ServerFrame {
            kind: Some(server_frame::Kind::Error(ErrorInfo {
                reason: 9,
                message: "overloaded".into(),
                retryable: true,
                upstream_code: String::new(),
            })),
        }]));
        let svc = ChatService::new(backend);
        let mut s = svc.run(sample_req(), CancellationToken::new()).await;
        let ev = s.next().await.expect("at least one event");
        match ev {
            InternalChatEvent::Error(e) => {
                assert_eq!(e.reason, FailoverReason::Overloaded);
                assert_eq!(e.message, "overloaded");
            }
            other => panic!("expected Error, got {other:?}"),
        }
        assert!(s.next().await.is_none());
    }

    #[test]
    fn binding_propagates_to_chat_start() {
        // When the caller supplies a ChannelBinding, build_chat_start must
        // copy every field verbatim and include the derived session_key.
        let mut req = sample_req();
        let b = ChannelBinding::qq_group(100, 200, 300);
        let expected_key = b.session_key();
        req.binding = Some(b);

        let start = build_chat_start(&req);
        let got = start.binding.expect("binding filled through");
        assert_eq!(got.channel, "qq");
        assert_eq!(got.account, "100");
        assert_eq!(got.thread, "200");
        assert_eq!(got.sender, "300");
        assert_eq!(got.session_key, expected_key);
    }

    #[test]
    fn absent_binding_stays_none() {
        let req = sample_req(); // binding: None
        let start = build_chat_start(&req);
        assert!(start.binding.is_none(), "None in → None out");
    }

    #[tokio::test]
    async fn cancel_aborts_with_error() {
        // Backend that yields once and then stalls forever.
        struct Stalling;
        #[async_trait]
        impl ChatBackendTrait for Stalling {
            async fn start(
                &self,
                _start: ChatStart,
            ) -> Result<(mpsc::Sender<ClientFrame>, BackendRx), CorlinmanError> {
                let (tx, _rx) = mpsc::channel::<ClientFrame>(8);
                let s = stream::pending::<Result<ServerFrame, CorlinmanError>>();
                Ok((tx, Box::pin(s)))
            }
        }
        let svc = ChatService::new(Arc::new(Stalling));
        let cancel = CancellationToken::new();
        let mut s = svc.run(sample_req(), cancel.clone()).await;
        cancel.cancel();
        let ev = tokio::time::timeout(std::time::Duration::from_secs(2), s.next())
            .await
            .expect("cancel unblocks stream")
            .expect("at least one event");
        assert!(matches!(ev, InternalChatEvent::Error(_)));
    }
}
