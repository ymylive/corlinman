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
pub mod registry;
pub mod runtime;
pub mod sandbox;

pub use discovery::{
    discover, roots_from_env_var, DiscoveredPlugin, DiscoveryDiagnostic, Origin, SearchRoot,
};
pub use manifest::{
    parse_manifest_file, Capabilities, Communication, EntryPoint, ManifestParseError,
    PluginManifest, PluginType, SandboxConfig, Tool,
};
pub use registry::{Diagnostic, PluginEntry, PluginRegistry};
