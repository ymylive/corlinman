//! `plugin-manifest.toml` schema — human-readable TOML, strict validation.
//!
//! corlinman's manifest is a fresh design. Every plugin ships a
//! `plugin-manifest.toml` in its own directory; we deserialise it into
//! [`PluginManifest`] and validate before registering.
//!
//! Taxonomy (plan §7.1):
//!   - `sync`    — JSON-RPC 2.0 over stdio, spawn-per-call, blocks until result.
//!   - `async`   — JSON-RPC 2.0 over stdio; response may carry `task_id` for
//!     later callback via `/plugin-callback`.
//!   - `service` — long-lived gRPC server; gateway launches once and reuses.
//!
//! Field conventions follow snake_case because TOML idiomatically uses
//! snake_case; serde's `rename_all = "snake_case"` keeps Rust field names
//! aligned with the TOML surface.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use validator::Validate;

pub use corlinman_core::manifest::Meta;

/// Top-level plugin manifest (`plugin-manifest.toml`).
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub struct PluginManifest {
    /// Stable identifier; lowercase letters / digits / dashes only.
    #[validate(length(min = 1, max = 128))]
    pub name: String,

    /// Semver-ish revision string.
    #[validate(length(min = 1, max = 32))]
    pub version: String,

    /// One-line description shown in `plugins list`.
    #[serde(default)]
    pub description: String,

    /// Author or maintainer email / handle.
    #[serde(default)]
    pub author: String,

    /// Plugin type dispatches the runtime choice.
    pub plugin_type: PluginType,

    /// How the gateway launches the plugin process.
    pub entry_point: EntryPoint,

    /// Transport knobs (currently just timeout).
    #[serde(default)]
    pub communication: Communication,

    /// Tool catalog + coarse flags.
    #[serde(default)]
    pub capabilities: Capabilities,

    /// Docker / resource limits. All optional.
    #[serde(default)]
    pub sandbox: SandboxConfig,

    /// UI "last touched" metadata (shared with core).
    #[serde(default)]
    pub meta: Option<Meta>,
}

/// Plugin runtime taxonomy. Three variants covering sync / async / service.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, JsonSchema, Default)]
#[serde(rename_all = "snake_case")]
pub enum PluginType {
    /// Spawn-per-call, blocks until result. JSON-RPC 2.0 over stdio.
    #[default]
    Sync,
    /// Spawn-per-call; may return `task_id` for out-of-band completion.
    Async,
    /// Long-lived gRPC service; gateway boots it once.
    Service,
}

impl PluginType {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Sync => "sync",
            Self::Async => "async",
            Self::Service => "service",
        }
    }
}

/// How to launch the plugin process. `cwd` is always the manifest's directory
/// (set by the runtime; not encoded in the manifest).
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate, Default)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub struct EntryPoint {
    /// Program to spawn. Resolved via PATH unless absolute.
    #[validate(length(min = 1))]
    pub command: String,

    /// Extra argv[1..]. Shell expansion is NOT performed.
    #[serde(default)]
    pub args: Vec<String>,

    /// Extra environment variables passed to the child. Keys should be
    /// uppercase alphanum / underscore; values are opaque strings.
    #[serde(default)]
    pub env: BTreeMap<String, String>,
}

/// Transport parameters. Only `timeout_ms` is meaningful today; additional
/// knobs (buffer sizes, heartbeat) can be added here without breaking changes.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Default)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub struct Communication {
    /// Hard deadline for a single invocation. Defaults to 30 000 ms.
    #[serde(default)]
    pub timeout_ms: Option<u64>,
}

/// Capability advertisement.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Default)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub struct Capabilities {
    /// Addressable tools. Each tool is invoked as `<plugin>.<tool>`.
    #[serde(default)]
    pub tools: Vec<Tool>,

    /// When true, models cannot invoke this plugin directly — it runs only
    /// via admin / operator flows.
    #[serde(default)]
    pub disable_model_invocation: bool,
}

/// A single tool exposed by the plugin. `parameters` is an inline JSON Schema
/// (any JSON-Schema-draft-07 shape).
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub struct Tool {
    #[validate(length(min = 1, max = 128))]
    pub name: String,

    #[serde(default)]
    pub description: String,

    /// JSON Schema (draft-07 style). Left as `serde_json::Value` so authors
    /// may use any combination of types / required / properties / $ref.
    #[serde(default = "default_empty_object")]
    pub parameters: serde_json::Value,
}

fn default_empty_object() -> serde_json::Value {
    serde_json::json!({ "type": "object" })
}

/// Sandbox config (plan §8). All fields are optional; runtimes that ignore
/// sandboxing simply don't read them.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Default)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub struct SandboxConfig {
    /// Memory cap as a docker-style string (`"256m"`, `"1g"`).
    #[serde(default)]
    pub memory: Option<String>,

    /// CPU fraction (e.g. `0.5`) or integer count.
    #[serde(default)]
    pub cpus: Option<f32>,

    /// Mount root FS read-only.
    #[serde(default)]
    pub read_only_root: bool,

    /// Linux capabilities to drop. `["ALL"]` is the common default.
    #[serde(default)]
    pub cap_drop: Vec<String>,

    /// Network mode: `"none"`, `"bridge"`, etc. Defaults to `"none"` if unset.
    #[serde(default)]
    pub network: Option<String>,

    /// Extra bind mounts in docker `src:dst[:ro]` syntax.
    #[serde(default)]
    pub binds: Vec<String>,
}

// ---------- Loader ----------

/// Error returned when `plugin-manifest.toml` fails to load.
#[derive(Debug, thiserror::Error)]
pub enum ManifestParseError {
    #[error("failed to read manifest {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("invalid TOML in manifest {path}: {source}")]
    Toml {
        path: PathBuf,
        #[source]
        source: toml::de::Error,
    },
    #[error("manifest {path} failed validation: {message}")]
    Validation { path: PathBuf, message: String },
}

/// Canonical filename the discovery layer looks for.
pub const MANIFEST_FILENAME: &str = "plugin-manifest.toml";

/// Parse a single manifest file from disk and validate it.
pub fn parse_manifest_file(path: &Path) -> Result<PluginManifest, ManifestParseError> {
    let raw = std::fs::read_to_string(path).map_err(|e| ManifestParseError::Io {
        path: path.to_path_buf(),
        source: e,
    })?;
    let manifest: PluginManifest = toml::from_str(&raw).map_err(|e| ManifestParseError::Toml {
        path: path.to_path_buf(),
        source: e,
    })?;
    manifest
        .validate()
        .map_err(|e| ManifestParseError::Validation {
            path: path.to_path_buf(),
            message: e.to_string(),
        })?;
    Ok(manifest)
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"
name = "greeter"
version = "0.1.0"
description = "Says hello"
author = "ada"
plugin_type = "sync"

[entry_point]
command = "python"
args = ["main.py"]

[communication]
timeout_ms = 5000

[[capabilities.tools]]
name = "greet"
description = "Greet someone"

[capabilities.tools.parameters]
type = "object"
required = ["name"]

[capabilities.tools.parameters.properties.name]
type = "string"

[capabilities]
disable_model_invocation = false

[sandbox]
memory = "256m"
cpus = 0.5
read_only_root = true
cap_drop = ["ALL"]
network = "none"
binds = []
"#;

    #[test]
    fn sample_manifest_parses() {
        let m: PluginManifest = toml::from_str(SAMPLE).unwrap();
        assert_eq!(m.name, "greeter");
        assert_eq!(m.version, "0.1.0");
        assert_eq!(m.plugin_type, PluginType::Sync);
        assert_eq!(m.entry_point.command, "python");
        assert_eq!(m.entry_point.args, vec!["main.py"]);
        assert_eq!(m.communication.timeout_ms, Some(5000));
        assert_eq!(m.capabilities.tools.len(), 1);
        assert_eq!(m.capabilities.tools[0].name, "greet");
        assert_eq!(m.sandbox.memory.as_deref(), Some("256m"));
        m.validate().unwrap();
    }

    #[test]
    fn empty_name_fails_validation() {
        let raw = r#"
name = ""
version = "0.1.0"
plugin_type = "sync"
[entry_point]
command = "true"
"#;
        let m: PluginManifest = toml::from_str(raw).unwrap();
        assert!(m.validate().is_err());
    }

    #[test]
    fn unknown_fields_rejected() {
        let raw = r#"
name = "x"
version = "0.1.0"
plugin_type = "sync"
mystery_field = 42
[entry_point]
command = "true"
"#;
        let err = toml::from_str::<PluginManifest>(raw).unwrap_err();
        assert!(err.to_string().contains("unknown field"), "{err}");
    }

    #[test]
    fn plugin_type_async_and_service_parse() {
        for t in ["async", "service"] {
            let raw = format!(
                "name = \"x\"\nversion = \"0.1.0\"\nplugin_type = \"{t}\"\n[entry_point]\ncommand = \"true\"\n"
            );
            let m: PluginManifest = toml::from_str(&raw).unwrap();
            match t {
                "async" => assert_eq!(m.plugin_type, PluginType::Async),
                "service" => assert_eq!(m.plugin_type, PluginType::Service),
                _ => unreachable!(),
            }
        }
    }

    #[test]
    fn parse_manifest_file_round_trip() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("plugin-manifest.toml");
        std::fs::write(&path, SAMPLE).unwrap();
        let m = parse_manifest_file(&path).unwrap();
        assert_eq!(m.name, "greeter");
    }
}
