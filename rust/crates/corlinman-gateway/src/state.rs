//! `AppState` — cloneable bundle of shared handles.
//!
//! Currently holds the plugin registry so handlers (notably the chat route)
//! can dispatch `ServerFrame::ToolCall` frames to real plugin runtimes via
//! [`corlinman_plugins::PluginRegistry`]. Later milestones will extend this
//! with the agent client, vector store, approval queue, and a broadcast
//! event bus (plan §14 R10).
//!
//! `config` + `config_path` landed with S5 T2: `Arc<ArcSwap<Config>>` gives
//! handlers a lock-free snapshot that the `POST /admin/config` endpoint can
//! swap in place, and `config_path` is the on-disk TOML the same endpoint
//! re-serialises to after a successful validate. Both are `Option` so the
//! existing `AppState::new(plugin_registry)` callsites (integration tests
//! that never touch config) stay valid — admin routes still carry their
//! own `AdminState` with a non-optional `config` for the admin surface.
//
// TODO: include `agent: corlinman_agent_client::AgentClient`,
//       `vector: corlinman_vector::Store`, `approvals: ApprovalQueue`, and a
//       broadcast `events: tokio::sync::broadcast::Sender<Event>`.

use std::path::PathBuf;
use std::sync::Arc;

use arc_swap::ArcSwap;
use corlinman_core::config::Config;
use corlinman_core::SessionStore;
use corlinman_plugins::runtime::service_grpc::ServiceRuntime;
use corlinman_plugins::{PluginRegistry, PluginSupervisor};

use crate::middleware::approval::ApprovalGate;

/// Process-wide shared handles. Cheap to clone — every field is `Arc`-wrapped.
#[derive(Clone)]
pub struct AppState {
    /// Discovered plugin manifests. Populated once at boot; later milestones
    /// will hot-reload via `notify`.
    pub plugin_registry: Arc<PluginRegistry>,
    /// Cross-request session history store. `None` only in bare stub builds
    /// that skip session persistence (e.g. some integration harnesses).
    pub session_store: Option<Arc<dyn SessionStore>>,
    /// Long-lived gRPC runtime that services `plugin_type = "service"` calls.
    /// `None` on stripped-down builds (tests, stub harnesses) that skip the
    /// supervisor boot step.
    pub service_runtime: Option<Arc<ServiceRuntime>>,
    /// Process lifecycle manager for service plugins; kept here so HTTP
    /// admin handlers (restart / stop) can reach into it later.
    pub plugin_supervisor: Option<Arc<PluginSupervisor>>,
    /// Tool-approval gate (Sprint 2 T3). `None` = no rules configured, so
    /// every tool call is admitted unchecked (matches pre-T3 behaviour).
    pub approval_gate: Option<Arc<ApprovalGate>>,
    /// Live gateway config snapshot (S5 T2). Readers call
    /// `state.config.as_ref().map(|c| c.load())` for a cheap, lock-free
    /// snapshot; `POST /admin/config` publishes swaps via `store`. `None`
    /// in the stub/test builds that never loaded a config file.
    pub config: Option<Arc<ArcSwap<Config>>>,
    /// On-disk location of the TOML that `config` was loaded from. Needed
    /// by `POST /admin/config` to atomically write the accepted payload
    /// back (tmp → rename). `None` when the config was synthesised from
    /// defaults rather than read from a file.
    pub config_path: Option<PathBuf>,
}

impl AppState {
    /// Build an `AppState` with the supplied registry. Callers wire this in
    /// from `main.rs` after discovery runs.
    pub fn new(plugin_registry: Arc<PluginRegistry>) -> Self {
        Self {
            plugin_registry,
            session_store: None,
            service_runtime: None,
            plugin_supervisor: None,
            approval_gate: None,
            config: None,
            config_path: None,
        }
    }

    /// Attach a session store. Fluent variant so `main.rs` can chain after
    /// opening `sessions.sqlite`.
    pub fn with_session_store(mut self, store: Arc<dyn SessionStore>) -> Self {
        self.session_store = Some(store);
        self
    }

    /// Attach the long-lived gRPC runtime + supervisor so service-type plugins
    /// dispatch through supervised child processes.
    pub fn with_service_stack(
        mut self,
        runtime: Arc<ServiceRuntime>,
        supervisor: Arc<PluginSupervisor>,
    ) -> Self {
        self.service_runtime = Some(runtime);
        self.plugin_supervisor = Some(supervisor);
        self
    }

    /// Attach the tool-approval gate so every `RegistryToolExecutor::execute`
    /// call consults the configured rules before dispatching.
    pub fn with_approval_gate(mut self, gate: Arc<ApprovalGate>) -> Self {
        self.approval_gate = Some(gate);
        self
    }

    /// Attach the shared live-config handle + its on-disk path. Fluent so
    /// `server.rs` can chain after loading `$CORLINMAN_CONFIG`.
    pub fn with_config(mut self, config: Arc<ArcSwap<Config>>, path: PathBuf) -> Self {
        self.config = Some(config);
        self.config_path = Some(path);
        self
    }

    /// Convenience constructor for tests / stubs that don't need any plugins.
    pub fn empty() -> Self {
        Self {
            plugin_registry: Arc::new(PluginRegistry::default()),
            session_store: None,
            service_runtime: None,
            plugin_supervisor: None,
            approval_gate: None,
            config: None,
            config_path: None,
        }
    }
}
