//! High-level [`AgentClient`] wrapping the tonic `AgentClient` stub.
//!
//! Resolves the Python agent endpoint from the environment:
//!   * `CORLINMAN_PY_SOCKET` — Unix socket path (preferred in production)
//!   * `CORLINMAN_PY_ADDR`   — explicit `host:port`
//!   * default TCP `127.0.0.1:50051`
//!
//! Only TCP is wired in this milestone (Unix sockets need a platform-specific
//! `Channel::from_static` bridge we'll add with the full deployment story).
//! The env is documented here so callers can rely on the precedence order.

use std::time::Duration;

use corlinman_core::CorlinmanError;
use corlinman_proto::v1::agent_client::AgentClient as ProtoAgentClient;
use tonic::transport::{Channel, Endpoint};

/// Default TCP address used when no env override is set.
pub const DEFAULT_TCP_ADDR: &str = "127.0.0.1:50051";

/// Resolve the Python agent endpoint from the environment.
///
/// Precedence mirrors `corlinman_server._bind_address`; we prefer TCP for the
/// Rust gateway (tonic's Unix socket support on macOS/Linux ships behind a
/// feature flag we'll opt into during Docker packaging).
pub fn resolve_endpoint() -> String {
    if let Ok(addr) = std::env::var("CORLINMAN_PY_ADDR") {
        return addr;
    }
    if let Ok(port) = std::env::var("CORLINMAN_PY_PORT") {
        return format!("127.0.0.1:{port}");
    }
    DEFAULT_TCP_ADDR.to_string()
}

/// Build a lazily-connecting tonic `Channel` targeting the Python agent.
///
/// Returns `CorlinmanError::Config` if the URI is malformed.
pub async fn connect_channel(endpoint: &str) -> Result<Channel, CorlinmanError> {
    let uri = if endpoint.starts_with("http://") || endpoint.starts_with("https://") {
        endpoint.to_string()
    } else {
        format!("http://{endpoint}")
    };
    let ep = Endpoint::from_shared(uri)
        .map_err(|e| CorlinmanError::Config(format!("invalid agent endpoint: {e}")))?
        .connect_timeout(Duration::from_secs(5))
        .tcp_nodelay(true);
    ep.connect()
        .await
        .map_err(|e| CorlinmanError::Config(format!("connect python agent: {e}")))
}

/// Thin high-level client: owns a clonable `Channel` and provides a typed
/// bidi stream opener.
#[derive(Clone)]
pub struct AgentClient {
    inner: ProtoAgentClient<Channel>,
}

impl AgentClient {
    /// Wrap an already-connected `Channel`.
    pub fn new(channel: Channel) -> Self {
        Self {
            inner: ProtoAgentClient::new(channel),
        }
    }

    /// Connect using `resolve_endpoint()`.
    pub async fn connect_default() -> Result<Self, CorlinmanError> {
        let ep = resolve_endpoint();
        let channel = connect_channel(&ep).await?;
        Ok(Self::new(channel))
    }

    /// Borrow the underlying generated client.
    pub fn inner_mut(&mut self) -> &mut ProtoAgentClient<Channel> {
        &mut self.inner
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[serial_test::serial]
    #[test]
    fn resolve_prefers_addr_env() {
        // SAFETY: single-threaded test, env mutation is fine.
        unsafe { std::env::set_var("CORLINMAN_PY_ADDR", "10.0.0.1:6000") };
        assert_eq!(resolve_endpoint(), "10.0.0.1:6000");
        unsafe { std::env::remove_var("CORLINMAN_PY_ADDR") };
    }

    #[serial_test::serial]
    #[test]
    fn resolve_falls_back_to_default() {
        unsafe { std::env::remove_var("CORLINMAN_PY_ADDR") };
        unsafe { std::env::remove_var("CORLINMAN_PY_PORT") };
        assert_eq!(resolve_endpoint(), DEFAULT_TCP_ADDR);
    }
}
