//! `AppState` — cloneable bundle of shared handles.
//!
//! Currently holds the plugin registry so handlers (notably the chat route)
//! can dispatch `ServerFrame::ToolCall` frames to real plugin runtimes via
//! [`corlinman_plugins::PluginRegistry`]. Later milestones will extend this
//! with live config, the agent client, vector store, approval queue, and a
//! broadcast event bus (plan §14 R10).
//
// TODO: hold `config: Arc<ArcSwap<CorlinmanConfig>>` for lock-free hot reload;
//       every handler calls `state.config.load()` at entry.
// TODO: include `agent: corlinman_agent_client::AgentClient`,
//       `vector: corlinman_vector::Store`, `approvals: ApprovalQueue`, and a
//       broadcast `events: tokio::sync::broadcast::Sender<Event>`.

use std::sync::Arc;

use corlinman_core::SessionStore;
use corlinman_plugins::PluginRegistry;

/// Process-wide shared handles. Cheap to clone — every field is `Arc`-wrapped.
#[derive(Clone)]
pub struct AppState {
    /// Discovered plugin manifests. Populated once at boot; later milestones
    /// will hot-reload via `notify`.
    pub plugin_registry: Arc<PluginRegistry>,
    /// Cross-request session history store. `None` only in bare stub builds
    /// that skip session persistence (e.g. some integration harnesses).
    pub session_store: Option<Arc<dyn SessionStore>>,
}

impl AppState {
    /// Build an `AppState` with the supplied registry. Callers wire this in
    /// from `main.rs` after discovery runs.
    pub fn new(plugin_registry: Arc<PluginRegistry>) -> Self {
        Self {
            plugin_registry,
            session_store: None,
        }
    }

    /// Attach a session store. Fluent variant so `main.rs` can chain after
    /// opening `sessions.sqlite`.
    pub fn with_session_store(mut self, store: Arc<dyn SessionStore>) -> Self {
        self.session_store = Some(store);
        self
    }

    /// Convenience constructor for tests / stubs that don't need any plugins.
    pub fn empty() -> Self {
        Self {
            plugin_registry: Arc::new(PluginRegistry::default()),
            session_store: None,
        }
    }
}
