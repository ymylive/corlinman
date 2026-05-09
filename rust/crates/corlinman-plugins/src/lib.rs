//! corlinman-plugins — manifest-first plugin lifecycle.
//!
//! Structure mirrors plan §2 and §7:
//!   - `discovery` + `manifest` + `registry` = load-time graph
//!   - `runtime/*` = execution contracts (JSON-RPC 2.0 stdio, gRPC service)
//!   - `sandbox/*` = Docker HostConfig assembly
//!   - `approval`, `async_task`, `preprocessor` = cross-cutting concerns

pub mod approval;
pub mod async_task;
pub mod discovery;
pub mod manifest;
pub mod preprocessor;
pub mod protocol;
pub mod registry;
pub mod runtime;
pub mod sandbox;
pub mod supervisor;

pub use async_task::{AsyncTaskRegistry, CompleteError};
pub use discovery::{
    discover, roots_from_env_var, DiscoveredPlugin, DiscoveryDiagnostic, Origin, SearchRoot,
};
pub use manifest::{
    parse_manifest_file, AllowlistMode, Capabilities, Communication, EntryPoint, EnvPassthrough,
    ManifestParseError, McpConfig, PluginManifest, PluginType, ResourcesAllowlist, RestartPolicy,
    SandboxConfig, Tool, ToolsAllowlist,
};
pub use registry::{Diagnostic, PluginEntry, PluginRegistry};
pub use supervisor::{PluginChild, PluginSupervisor};
