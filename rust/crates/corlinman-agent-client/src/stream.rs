//! Bidi stream adapter with backpressure.
//!
//! The gateway owns the **client side** of `Agent.Chat`:
//!   - writes [`ClientFrame`]s (ChatStart, ToolResult, Cancel, ApprovalDecision)
//!     into a bounded `mpsc::Sender<ClientFrame>(16)`
//!   - reads [`ServerFrame`]s (TokenDelta, ToolCall, AwaitingApproval, Done,
//!     ErrorInfo) from the gRPC response stream
//!
//! On drop the sender is closed, which cooperatively ends the Python side via
//! the `async for frame in stream:` Pythonic exit. Callers that want eager
//! cancellation should call [`ChatStream::cancel`] before dropping.

use corlinman_core::CorlinmanError;
use corlinman_proto::v1::{client_frame, ClientFrame, ServerFrame};
use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;
use tonic::{Request, Streaming};

use crate::client::AgentClient;
use crate::retry::status_to_error;

/// Outbound capacity for ClientFrame queue (plan §5.1 "mpsc(16) both sides").
pub const CHANNEL_CAPACITY: usize = 16;

/// Paired sender + receive stream for a single `Agent.Chat` call.
pub struct ChatStream {
    /// Sender the gateway writes into.
    pub tx: mpsc::Sender<ClientFrame>,
    /// The gRPC server-stream response.
    pub rx: Streaming<ServerFrame>,
}

impl ChatStream {
    /// Open a new bidirectional stream against `client`, returning the paired
    /// sender + receive handle. The caller is responsible for sending the
    /// first `ChatStart` frame.
    pub async fn open(client: &mut AgentClient) -> Result<Self, CorlinmanError> {
        let (tx, rx) = mpsc::channel::<ClientFrame>(CHANNEL_CAPACITY);
        let outbound = ReceiverStream::new(rx);
        let response = client
            .inner_mut()
            .chat(Request::new(outbound))
            .await
            .map_err(status_to_error)?;
        Ok(Self {
            tx,
            rx: response.into_inner(),
        })
    }

    /// Best-effort cancel: pushes a `Cancel { reason }` into the queue.
    ///
    /// Returns whether the frame was accepted (false if the peer already
    /// closed the stream).
    pub async fn cancel(&self, reason: &str) -> bool {
        let frame = ClientFrame {
            kind: Some(client_frame::Kind::Cancel(corlinman_proto::v1::Cancel {
                reason: reason.to_string(),
            })),
        };
        self.tx.send(frame).await.is_ok()
    }

    /// Convert the incoming stream into a [`Result<ServerFrame, CorlinmanError>`]
    /// by classifying tonic `Status` into the shared error taxonomy. Consumes
    /// the `rx` half; `tx` remains with the caller for sending follow-up
    /// frames (ToolResult, ApprovalDecision, Cancel).
    pub async fn next_classified(&mut self) -> Option<Result<ServerFrame, CorlinmanError>> {
        match self.rx.message().await {
            Ok(Some(frame)) => Some(Ok(frame)),
            Ok(None) => None,
            Err(status) => Some(Err(status_to_error(status))),
        }
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn channel_capacity_is_16() {
        assert_eq!(super::CHANNEL_CAPACITY, 16);
    }
}
