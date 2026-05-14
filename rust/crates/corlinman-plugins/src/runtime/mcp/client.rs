//! Stdio-framed MCP client used by the plugin adapter.
//!
//! The C1 crate (`corlinman-mcp`) ships an outbound stdio peer in its
//! `client::stdio` module, but two factors make us reimplement here:
//!
//!   1. The `corlinman_mcp::client` module is *not* re-exported from
//!      `corlinman_mcp::lib`, so the type isn't reachable from
//!      downstream crates without modifying corlinman-mcp (read-only
//!      for C2 by task contract).
//!   2. The C1 implementation has a known correctness bug — its
//!      `swap_pending` is a no-op, so the writer's `pending` map and
//!      the reader's `pending_for_reader` arc are not the same map.
//!      Multiplexed concurrent calls would silently drop responses.
//!      Iter 5 needs concurrent-call correctness; designing around
//!      that bug is more code than just owning the client here.
//!
//! What we keep from C1: the *wire schema* — every request and
//! response on the wire is built out of `corlinman_mcp::schema::*`
//! types. So when the upstream MCP `2024-11-05` schema evolves the
//! schema crate is the only place that needs to move.
//!
//! What we keep from `runtime::mcp_stdio`: the spawn primitive
//! (`spawn_mcp_child`) — child lifecycle, blank-env scoping,
//! `kill_on_drop`. This module owns only the framing + demux on top.

use std::collections::HashMap;
use std::ffi::OsString;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use serde_json::Value as JsonValue;
use thiserror::Error;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::sync::{mpsc, oneshot, Mutex};
use tokio::task::JoinHandle;
use tracing::{debug, warn};

use crate::runtime::mcp::schema::{
    error_codes, JsonRpcError, JsonRpcRequest, JsonRpcResponse, JSONRPC_VERSION,
};
use crate::runtime::mcp_stdio::{spawn_mcp_child, McpChild, SpawnError};

/// Errors returned by [`McpStdioClient`].
#[derive(Debug, Error)]
pub enum ClientError {
    /// The child failed to spawn (binary missing, bad cwd, etc.).
    #[error("spawn: {0}")]
    Spawn(#[from] SpawnError),

    /// Failed to write a frame to the child's stdin (writer task exited).
    #[error("writer task closed: {0}")]
    WriterClosed(String),

    /// The child exited / stdout closed before the response arrived.
    #[error("connection closed before reply: {0}")]
    Disconnected(String),

    /// Server returned a JSON-RPC error frame.
    #[error("server error code {code}: {message}")]
    ServerError {
        code: i32,
        message: String,
        data: Option<JsonValue>,
    },

    /// Awaiting the response exceeded the supplied deadline.
    #[error("call timed out after {millis}ms")]
    Timeout { millis: u64 },

    /// Internal serde failure — should not happen with statically-typed inputs.
    #[error("serde: {0}")]
    Serde(#[from] serde_json::Error),
}

/// Stdio-framed MCP client. Wraps a single spawned child; cheap to
/// `Arc::clone` for shared use across the adapter's call multiplex.
///
/// Threading model: one writer task and one reader task per client;
/// `call` is `&self`, so any number of in-flight requests may share
/// the same client. Each request gets a unique id and parks on a
/// `oneshot::Receiver` keyed by that id; the reader resolves the
/// matching oneshot when it parses the response off the wire.
#[derive(Clone)]
pub struct McpStdioClient {
    inner: Arc<Inner>,
}

struct Inner {
    /// Outbound frame queue → writer task → child stdin.
    tx: mpsc::Sender<String>,
    /// Pending request map: id (canonical-string) → oneshot for response.
    pending: Arc<Mutex<HashMap<String, oneshot::Sender<JsonRpcResponse>>>>,
    /// Atomic counter for client-side id generation.
    next_id: AtomicU64,
    /// Owned background tasks; aborted on Drop of the last clone.
    reader: Mutex<Option<JoinHandle<()>>>,
    writer: Mutex<Option<JoinHandle<()>>>,
    /// Notifier fired exactly once when the reader task observes EOF
    /// or the writer task encounters a broken pipe — i.e. the child
    /// is gone and no more responses will arrive. Iter 6's supervisor
    /// awaits this to learn about crashes without polling.
    disconnect: Arc<tokio::sync::Notify>,
}

impl std::fmt::Debug for McpStdioClient {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("McpStdioClient")
            .field("strong_count", &Arc::strong_count(&self.inner))
            .finish()
    }
}

/// Marker prefix the reader uses to tunnel "stdout closed" through the
/// `JsonRpcResponse` channel back into a typed `ClientError::Disconnected`.
const DISCONNECTED_MARKER: &str = "__mcp_disconnected__:";

impl McpStdioClient {
    /// Spawn `command` with `args` in `cwd`, hooking stdio to the
    /// MCP framing layer. `env` is the already-filtered allow/deny
    /// passthrough output; the spawn primitive layers the four
    /// always-required keys (`PATH`/`HOME`/`USER`/`LANG`) underneath.
    pub fn connect_stdio(
        command: &str,
        args: &[String],
        cwd: &Path,
        env: Vec<(OsString, OsString)>,
    ) -> Result<Self, ClientError> {
        let mut child = spawn_mcp_child(command, args, cwd, env)?;
        let stdin = child
            .take_stdin()
            .ok_or_else(|| ClientError::Disconnected("child stdin pipe missing".into()))?;
        let stdout = child
            .take_stdout()
            .ok_or_else(|| ClientError::Disconnected("child stdout pipe missing".into()))?;
        // We deliberately drop stderr here; the adapter (this module's
        // caller) is responsible for capturing it into tracing if it
        // wants to. Iter 4's scope is handshake; stderr capture is iter 6+.
        let _stderr = child.take_stderr();

        Ok(Self::wire_up(child, stdin, stdout))
    }

    fn wire_up(
        child: McpChild,
        mut stdin: impl AsyncWriteExt + Unpin + Send + 'static,
        stdout: impl tokio::io::AsyncRead + Unpin + Send + 'static,
    ) -> Self {
        let (tx, mut rx) = mpsc::channel::<String>(64);
        let pending: Arc<Mutex<HashMap<String, oneshot::Sender<JsonRpcResponse>>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let disconnect = Arc::new(tokio::sync::Notify::new());
        let disconnect_for_writer = Arc::clone(&disconnect);
        let disconnect_for_reader = Arc::clone(&disconnect);

        // Writer: drain mpsc, append \n, write to child stdin. We
        // append the newline here so misbehaving callers can't break
        // framing with embedded newlines.
        //
        // Fail-fast: if a stdin write errors (broken pipe → child
        // gone), we drain `pending` ourselves so callers don't park
        // until their deadline. The reader does the same thing on
        // EOF; either path is sufficient and they're idempotent
        // (whichever fires first claims the entries).
        let pending_for_writer = Arc::clone(&pending);
        let writer = tokio::spawn(async move {
            'outer: while let Some(frame) = rx.recv().await {
                if let Err(err) = stdin.write_all(frame.as_bytes()).await {
                    warn!(%err, "mcp client: stdin write_all failed");
                    break 'outer;
                }
                if let Err(err) = stdin.write_all(b"\n").await {
                    warn!(%err, "mcp client: stdin newline write failed");
                    break 'outer;
                }
                if let Err(err) = stdin.flush().await {
                    warn!(%err, "mcp client: stdin flush failed");
                    break 'outer;
                }
            }
            // Drain pending: synthesise a Disconnected response for
            // each parked oneshot.
            let mut p = pending_for_writer.lock().await;
            for (_id, waiter) in p.drain() {
                let resp = JsonRpcResponse::err(
                    JsonValue::Null,
                    JsonRpcError::new(
                        error_codes::INTERNAL_ERROR,
                        format!("{}stdin write failed", DISCONNECTED_MARKER),
                    ),
                );
                let _ = waiter.send(resp);
            }
            // Fire disconnect *after* the drain so any awaiter that
            // wakes on the notify finds an empty pending map and a
            // closed mpsc — i.e. observably dead.
            disconnect_for_writer.notify_waiters();
            debug!("mcp client: writer task exiting");
        });

        // Reader: line-delimited stdout → parse JsonRpcResponse →
        // resolve the parked oneshot keyed by id.
        let pending_for_reader = Arc::clone(&pending);
        let reader = tokio::spawn(async move {
            let mut lines = BufReader::new(stdout).lines();
            loop {
                match lines.next_line().await {
                    Ok(Some(line)) => {
                        if line.trim().is_empty() {
                            continue;
                        }
                        match serde_json::from_str::<JsonRpcResponse>(&line) {
                            Ok(resp) => {
                                let key = id_key(resp.id());
                                let waiter = {
                                    let mut p = pending_for_reader.lock().await;
                                    p.remove(&key)
                                };
                                if let Some(tx) = waiter {
                                    let _ = tx.send(resp);
                                } else {
                                    debug!(id = %key, "mcp client: dropped unmatched response");
                                }
                            }
                            Err(err) => {
                                warn!(%err, line = %line, "mcp client: parse failed");
                            }
                        }
                    }
                    Ok(None) => {
                        debug!("mcp client: stdout EOF");
                        break;
                    }
                    Err(err) => {
                        warn!(%err, "mcp client: stdout read error");
                        break;
                    }
                }
            }
            // Fail every parked waiter so callers don't hang.
            let mut p = pending_for_reader.lock().await;
            for (_id, waiter) in p.drain() {
                let resp = JsonRpcResponse::err(
                    JsonValue::Null,
                    JsonRpcError::new(
                        error_codes::INTERNAL_ERROR,
                        format!("{}stdout closed", DISCONNECTED_MARKER),
                    ),
                );
                let _ = waiter.send(resp);
            }
            disconnect_for_reader.notify_waiters();
            // Drop the McpChild last — kill_on_drop catches stragglers.
            drop(child);
        });

        Self {
            inner: Arc::new(Inner {
                tx,
                pending,
                next_id: AtomicU64::new(0),
                reader: Mutex::new(Some(reader)),
                writer: Mutex::new(Some(writer)),
                disconnect,
            }),
        }
    }

    /// Future that resolves once the client observes a disconnect
    /// (writer mpsc broken, reader stdout EOF, or both). Used by the
    /// supervisor (iter 6) to wake on child exits without polling.
    pub async fn wait_disconnect(&self) {
        // Tokio's `Notify::notified` only catches notifications that
        // arrive *after* it has been registered. To avoid a race where
        // the disconnect fired before `wait_disconnect` was called, we
        // also do a fast-path probe: if the writer mpsc is already
        // closed, return immediately.
        let notified = self.inner.disconnect.notified();
        tokio::pin!(notified);
        if self.inner.tx.is_closed() {
            return;
        }
        notified.await;
    }

    /// Generate the next request id (`req-N` shape).
    fn next_id(&self) -> JsonValue {
        let n = self.inner.next_id.fetch_add(1, Ordering::Relaxed);
        JsonValue::String(format!("req-{n}"))
    }

    /// Send a request and wait for the response, optionally bounded by
    /// `deadline`. Generates a fresh id; the reader matches by id.
    pub async fn call(
        &self,
        method: impl Into<String>,
        params: JsonValue,
        deadline: Option<Duration>,
    ) -> Result<JsonValue, ClientError> {
        let id = self.next_id();
        let key = id_key(&id);
        let (tx, rx) = oneshot::channel();
        {
            let mut p = self.inner.pending.lock().await;
            p.insert(key.clone(), tx);
        }

        let req = JsonRpcRequest {
            jsonrpc: JSONRPC_VERSION.to_string(),
            id: Some(id.clone()),
            method: method.into(),
            params,
        };
        let frame = serde_json::to_string(&req)?;
        if self.inner.tx.send(frame).await.is_err() {
            self.inner.pending.lock().await.remove(&key);
            return Err(ClientError::WriterClosed(
                "child writer task exited before request".into(),
            ));
        }

        let resp = match deadline {
            None => rx
                .await
                .map_err(|_| ClientError::Disconnected("oneshot canceled".into()))?,
            Some(d) => match tokio::time::timeout(d, rx).await {
                Ok(Ok(r)) => r,
                Ok(Err(_)) => return Err(ClientError::Disconnected("oneshot canceled".into())),
                Err(_) => {
                    // Time out: pull our entry from the pending map so
                    // a late response doesn't try to fire the dropped tx.
                    self.inner.pending.lock().await.remove(&key);
                    return Err(ClientError::Timeout {
                        millis: d.as_millis() as u64,
                    });
                }
            },
        };

        match resp {
            JsonRpcResponse::Result { result, .. } => Ok(result),
            JsonRpcResponse::Error { error, .. } => {
                if let Some(rest) = error.message.strip_prefix(DISCONNECTED_MARKER) {
                    return Err(ClientError::Disconnected(rest.to_string()));
                }
                Err(ClientError::ServerError {
                    code: error.code,
                    message: error.message,
                    data: error.data,
                })
            }
        }
    }

    /// Send a notification (no id, no response).
    pub async fn notify(
        &self,
        method: impl Into<String>,
        params: JsonValue,
    ) -> Result<(), ClientError> {
        let req = JsonRpcRequest {
            jsonrpc: JSONRPC_VERSION.to_string(),
            id: None,
            method: method.into(),
            params,
        };
        let frame = serde_json::to_string(&req)?;
        self.inner
            .tx
            .send(frame)
            .await
            .map_err(|_| ClientError::WriterClosed("child writer task exited".into()))?;
        Ok(())
    }

    /// True iff the writer mpsc is still accepting frames. Cheap probe
    /// the adapter uses to detect a child that has crashed.
    pub fn is_alive(&self) -> bool {
        !self.inner.tx.is_closed()
    }

    /// Explicitly tear down the client: abort the reader/writer tasks
    /// and fire the disconnect notify so any waiter wakes up. Safe to
    /// call multiple times (subsequent calls are no-ops because the
    /// `Mutex<Option<JoinHandle>>` slots are emptied). Independent of
    /// `Drop`'s "last clone" gate — the supervisor calls this when
    /// the operator triggered a stop, so even with other clones in
    /// flight the child dies.
    ///
    /// Reader/writer tasks own the `McpChild`; their cancellation
    /// drops the child; `kill_on_drop` propagates SIGKILL to the
    /// process.
    pub async fn shutdown(&self) {
        if let Some(h) = self.inner.reader.lock().await.take() {
            h.abort();
        }
        if let Some(h) = self.inner.writer.lock().await.take() {
            h.abort();
        }
        // Also drain pending so any in-flight call doesn't hang
        // waiting for a response that's never coming.
        let mut p = self.inner.pending.lock().await;
        for (_id, waiter) in p.drain() {
            let resp = JsonRpcResponse::err(
                JsonValue::Null,
                JsonRpcError::new(
                    error_codes::INTERNAL_ERROR,
                    format!("{}explicit shutdown", DISCONNECTED_MARKER),
                ),
            );
            let _ = waiter.send(resp);
        }
        self.inner.disconnect.notify_waiters();
    }
}

impl Drop for McpStdioClient {
    fn drop(&mut self) {
        // Last clone? Abort worker tasks. The owned `McpChild` lives
        // inside the reader closure; once aborted, kill_on_drop on the
        // child takes care of the process.
        if Arc::strong_count(&self.inner) == 1 {
            if let Ok(mut g) = self.inner.reader.try_lock() {
                if let Some(h) = g.take() {
                    h.abort();
                }
            }
            if let Ok(mut g) = self.inner.writer.try_lock() {
                if let Some(h) = g.take() {
                    h.abort();
                }
            }
        }
    }
}

/// Canonical-string id for HashMap keying (JSON-RPC ids may be string
/// or number).
fn id_key(id: &JsonValue) -> String {
    match id {
        JsonValue::String(s) => s.clone(),
        JsonValue::Number(n) => n.to_string(),
        JsonValue::Null => "null".to_string(),
        other => other.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Build a sh-piped client that turns each input request frame into
    /// a result frame echoing the request id. Used by handshake-shape
    /// tests in this module and by `adapter` tests in iter 5.
    pub(crate) async fn echo_id_responder() -> McpStdioClient {
        let tmp = tempfile::tempdir().unwrap();
        let env =
            crate::runtime::mcp_stdio::build_child_env(std::iter::empty::<(String, String)>());
        // Keep child alive across calls but echo each request id back
        // as a result frame. awk works on every BSD/Linux box.
        let mut child = crate::runtime::mcp_stdio::spawn_mcp_child(
            "sh",
            &[
                "-c".to_string(),
                r#"awk 'BEGIN{FS=","} {
                    for (i=1;i<=NF;i++) if ($i ~ /"id"/) idline=$i;
                    gsub(/.*"id":/, "", idline);
                    gsub(/[}\]].*/, "", idline);
                    printf("{\"jsonrpc\":\"2.0\",\"id\":%s,\"result\":{\"echo\":true}}\n", idline);
                    fflush();
                }'"#
                .to_string(),
            ],
            tmp.path(),
            env,
        )
        .expect("spawn awk responder");

        let stdin = child.take_stdin().unwrap();
        let stdout = child.take_stdout().unwrap();
        let _stderr = child.take_stderr();
        // Hold tmp alive: leak it so the `cwd` survives for the lifetime
        // of the responder. Acceptable in tests; the OS reclaims on
        // process exit.
        std::mem::forget(tmp);
        McpStdioClient::wire_up(child, stdin, stdout)
    }

    #[tokio::test]
    async fn call_resolves_against_echo_responder() {
        let client = echo_id_responder().await;
        let r = client
            .call("ping", JsonValue::Null, Some(Duration::from_secs(2)))
            .await
            .expect("call must succeed");
        assert_eq!(r, serde_json::json!({"echo": true}));
    }

    #[tokio::test]
    async fn timeout_returns_typed_error_when_child_never_replies() {
        if which::which("sleep").is_err() {
            eprintln!("sleep not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let env =
            crate::runtime::mcp_stdio::build_child_env(std::iter::empty::<(String, String)>());
        // sleep never reads stdin; perfect "ignores requests" model.
        let client = McpStdioClient::connect_stdio("sleep", &["10".to_string()], tmp.path(), env)
            .expect("spawn sleep");
        let err = client
            .call(
                "initialize",
                JsonValue::Null,
                Some(Duration::from_millis(50)),
            )
            .await
            .expect_err("must time out");
        assert!(matches!(err, ClientError::Timeout { .. }), "got {err:?}");
    }

    /// When the child exits while a request is in flight, `call`
    /// must NOT hang. The race between writer-mpsc closure and reader
    /// EOF means we may surface either `Disconnected`, `WriterClosed`,
    /// or a deadline `Timeout` — all three are acceptable proofs that
    /// the connection died; what we never accept is a hang past the
    /// caller's deadline.
    #[tokio::test]
    async fn disconnect_propagates_to_call() {
        if which::which("true").is_err() {
            eprintln!("`true` not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let env =
            crate::runtime::mcp_stdio::build_child_env(std::iter::empty::<(String, String)>());
        let client =
            McpStdioClient::connect_stdio("true", &[], tmp.path(), env).expect("spawn true");
        // Give the reader a moment to observe EOF.
        tokio::time::sleep(Duration::from_millis(50)).await;
        let err = client
            .call("ping", JsonValue::Null, Some(Duration::from_millis(500)))
            .await
            .expect_err("must surface a typed error rather than hang");
        assert!(
            matches!(
                err,
                ClientError::Disconnected(_)
                    | ClientError::WriterClosed(_)
                    | ClientError::Timeout { .. }
            ),
            "got {err:?}"
        );
    }
}
