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
    /// Manifest schema version. Absent/0/1 = v1 (legacy); 2 = v2+.
    #[serde(default = "default_manifest_version")]
    pub manifest_version: u32,

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

    /// MCP-adapter knobs. Only meaningful (and only allowed) when
    /// `plugin_type = "mcp"` AND `manifest_version >= 3`. v3 schema, see
    /// [`McpConfig`].
    #[serde(default)]
    pub mcp: Option<McpConfig>,

    /// UI "last touched" metadata (shared with core).
    #[serde(default)]
    pub meta: Option<Meta>,

    /// Tool-call protocols the plugin can accept. Default: `["openai_function"]`.
    #[serde(default = "default_protocols")]
    pub protocols: Vec<String>,

    /// Hook events this plugin subscribes to (matches
    /// `corlinman-hooks` HookEvent kinds).
    #[serde(default)]
    pub hooks: Vec<String>,

    /// Skill identifiers this plugin participates in.
    #[serde(default)]
    pub skill_refs: Vec<String>,
}

fn default_manifest_version() -> u32 {
    1
}

fn default_protocols() -> Vec<String> {
    vec!["openai_function".into()]
}

/// Protocols we currently accept. Additions require a coordinated
/// gateway+plugin rollout.
const KNOWN_PROTOCOLS: &[&str] = &["openai_function", "block"];

/// Hook event kinds known as of the B1 plan. Unknown names are a warning,
/// not an error — manifests may reference hooks that ship in a later
/// gateway version (forward-compat).
const KNOWN_HOOK_EVENTS: &[&str] = &[
    "message.received",
    "message.sent",
    "message.transcribed",
    "message.preprocessed",
    "session.patch",
    "agent.bootstrap",
    "gateway.startup",
    "config.changed",
];

/// Highest schema version this gateway understands. Manifests declaring a
/// higher value are rejected with a forward-compat hint.
const MAX_SUPPORTED_MANIFEST_VERSION: u32 = 3;

/// First manifest version that recognises `plugin_type = "mcp"` and the
/// `[mcp]` table. v2 manifests setting either are a hard validation error.
const MCP_MIN_MANIFEST_VERSION: u32 = 3;

/// Plugin runtime taxonomy. Four variants covering sync / async / service / mcp.
///
/// `Mcp` requires `manifest_version >= 3` and an accompanying `[mcp]` table;
/// see `validate_all` for the cross-field check.
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
    /// MCP (Model Context Protocol) stdio server consumed as a corlinman
    /// tool source. Long-lived child; multiplexed JSON-RPC over the same
    /// stdio connection. v3-only.
    Mcp,
}

impl PluginType {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Sync => "sync",
            Self::Async => "async",
            Self::Service => "service",
            Self::Mcp => "mcp",
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

// ---------- MCP (manifest v3) ----------

/// `[mcp]` block — only honoured when `plugin_type = "mcp"` and
/// `manifest_version >= 3`.
///
/// The defaults make a manifest with a bare `plugin_type = "mcp"` +
/// `[entry_point]` work out of the box: lazy spawn, restart on crash with
/// the same ceiling as the existing supervisor, no env passthrough at all,
/// no tools exported (fail-closed allowlist), no resources exported.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Default)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub struct McpConfig {
    /// Spawn at gateway boot when true; otherwise spawn lazily on first
    /// dispatch. Default: false (lazy).
    #[serde(default)]
    pub autostart: bool,

    /// What to do on child exit. See [`RestartPolicy`].
    #[serde(default)]
    pub restart_policy: RestartPolicy,

    /// Crash-loop circuit breaker — N crashes inside `crash_loop_window_secs`
    /// flips the entry to `failed`. Mirrors `supervisor::SupervisorConfig`.
    /// Default: 3.
    #[serde(default = "default_crash_loop_max")]
    pub crash_loop_max: u32,

    /// Crash-loop window in seconds. Default: 60.
    #[serde(default = "default_crash_loop_window_secs")]
    pub crash_loop_window_secs: u64,

    /// MCP `initialize` round-trip deadline in milliseconds. Default: 5000.
    #[serde(default = "default_handshake_timeout_ms")]
    pub handshake_timeout_ms: u64,

    /// Idle-shutdown grace period in seconds. `0` = never auto-shutdown.
    /// Default: 0.
    #[serde(default)]
    pub idle_shutdown_secs: u64,

    /// Env passthrough rules for the spawned child. The MCP runtime starts
    /// from a *blank* env (plus the four required `PATH/HOME/USER/LANG`)
    /// and copies only allowlisted names.
    #[serde(default)]
    pub env_passthrough: EnvPassthrough,

    /// Filter applied to upstream `tools/list`. Defaults to
    /// `mode = "allow"` with empty `names` — so a freshly authored
    /// manifest exports zero tools until the operator opts in.
    #[serde(default)]
    pub tools_allowlist: ToolsAllowlist,

    /// Filter applied to upstream `resources/*`. Reserved for a later
    /// iteration; see `phase4-w3-c2-design.md` open question 1.
    #[serde(default)]
    pub resources_allowlist: ResourcesAllowlist,
}

fn default_crash_loop_max() -> u32 {
    3
}

fn default_crash_loop_window_secs() -> u64 {
    60
}

fn default_handshake_timeout_ms() -> u64 {
    5_000
}

/// What to do when the MCP child exits.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, JsonSchema, Default)]
#[serde(rename_all = "snake_case")]
pub enum RestartPolicy {
    /// Don't respawn; admin must hit `/restart` to revive.
    Never,
    /// Respawn only when the exit was non-zero (or signalled). Default.
    #[default]
    OnCrash,
    /// Respawn on any exit, including clean shutdown.
    Always,
}

impl RestartPolicy {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Never => "never",
            Self::OnCrash => "on_crash",
            Self::Always => "always",
        }
    }
}

/// Env-var passthrough rules.
///
/// `allow` is the canonical surface: nothing leaks to the child unless its
/// exact name is here. `deny` is a glob over the allow set — defence in
/// depth so an operator who later writes `allow = ["*"]` still keeps known
/// secret prefixes (e.g. `AWS_*`) out.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Default)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub struct EnvPassthrough {
    /// Exact-name env vars to forward. Empty = none.
    #[serde(default)]
    pub allow: Vec<String>,

    /// Glob patterns evaluated against the allow set; matching names
    /// are dropped. Empty = no extra filter.
    #[serde(default)]
    pub deny: Vec<String>,
}

/// Allowlist mode for MCP tool names.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, JsonSchema, Default)]
#[serde(rename_all = "snake_case")]
pub enum AllowlistMode {
    /// Export only names listed in `names`. Empty list = export nothing.
    /// Default — fail-closed so a fresh manifest exports zero tools.
    #[default]
    Allow,
    /// Export every upstream tool except those in `names`.
    Deny,
    /// Export every upstream tool unconditionally. The opt-in escape hatch.
    All,
}

impl AllowlistMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Allow => "allow",
            Self::Deny => "deny",
            Self::All => "all",
        }
    }
}

/// Filter applied to upstream `tools/list`.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Default)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub struct ToolsAllowlist {
    /// Selection mode; default `allow`.
    #[serde(default)]
    pub mode: AllowlistMode,

    /// Exact tool names from the upstream MCP server. Semantics depend
    /// on `mode`. Ignored when `mode = "all"`.
    #[serde(default)]
    pub names: Vec<String>,
}

/// Filter applied to upstream `resources/*`. Reserved.
///
/// `patterns` are URI globs (e.g. `file:///etc/**`). The adapter
/// rejects manifests with a non-default `mode`/`patterns` until
/// resource support lands; the field exists only to keep the v3
/// shape stable so the adapter can fill it in without a v4 bump.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Default)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub struct ResourcesAllowlist {
    /// Selection mode; default `allow` (matching nothing because patterns
    /// is empty by default).
    #[serde(default)]
    pub mode: AllowlistMode,

    /// URI glob patterns.
    #[serde(default)]
    pub patterns: Vec<String>,
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

impl PluginManifest {
    /// Upgrade a freshly-deserialised manifest to the current in-memory
    /// shape (v3 today).
    ///
    /// The on-disk file is **not** rewritten: legacy manifests continue to
    /// parse in place, and the gateway merely fills in the new fields with
    /// their documented defaults so downstream code sees a uniform v3
    /// structure.
    ///
    /// Migration steps (cumulative, idempotent):
    ///   - v1 -> v2: bump `manifest_version` (protocols/hooks/skill_refs
    ///     get serde defaults).
    ///   - v2 -> v3: bump `manifest_version`. The new `[mcp]` field stays
    ///     `None` for non-MCP plugins; MCP plugins must author a v3
    ///     manifest themselves (validation rejects mcp-on-v2).
    pub fn migrate_to_current_in_memory(&mut self) {
        if self.manifest_version < 2 {
            tracing::debug!(
                "manifest {name} loaded as v1; upgrading in-memory to v2 shape",
                name = self.name
            );
            self.manifest_version = 2;
        }
        if self.manifest_version < 3 {
            // v2 -> v3 is a no-op for non-MCP plugins; the [mcp] table
            // defaults to None which is correct for sync/async/service.
            // For MCP-flavoured manifests (`plugin_type = "mcp"` or an
            // explicit `[mcp]` block), we deliberately leave the version
            // stamp alone so `validate_all` can surface the version-bump
            // error instead of silently masking the misconfiguration.
            let is_mcp_flavoured = self.plugin_type == PluginType::Mcp || self.mcp.is_some();
            if !is_mcp_flavoured {
                tracing::debug!(
                    "manifest {name} loaded as v{prev}; upgrading in-memory to v3 shape",
                    name = self.name,
                    prev = self.manifest_version
                );
                self.manifest_version = 3;
            }
        }
    }

    /// Backwards-compatible alias for `migrate_to_current_in_memory`.
    #[deprecated(note = "use `migrate_to_current_in_memory` — v3 supersedes v2")]
    pub fn migrate_to_v2_in_memory(&mut self) {
        self.migrate_to_current_in_memory();
    }

    /// Run both the derive-based field validation and the cross-field
    /// rules (protocol whitelist, version ceiling, hook name advisory
    /// warnings, MCP version gating).
    pub fn validate_all(&self) -> Result<(), String> {
        self.validate().map_err(|e| e.to_string())?;

        if self.manifest_version == 0 || self.manifest_version > MAX_SUPPORTED_MANIFEST_VERSION {
            return Err(format!(
                "manifest_version {} is not supported (this gateway supports 1..={}); \
                 upgrade the gateway to load newer manifests",
                self.manifest_version, MAX_SUPPORTED_MANIFEST_VERSION
            ));
        }

        for proto in &self.protocols {
            if !KNOWN_PROTOCOLS.contains(&proto.as_str()) {
                return Err(format!(
                    "unknown protocol {:?}; allowed: {:?}",
                    proto, KNOWN_PROTOCOLS
                ));
            }
        }

        for hook in &self.hooks {
            if !KNOWN_HOOK_EVENTS.contains(&hook.as_str()) {
                tracing::warn!(
                    plugin = %self.name,
                    hook = %hook,
                    "manifest references unknown hook event; treating as forward-compat \
                     (will no-op until a hook source emits this kind)"
                );
            }
        }

        // MCP / v3 cross-field rules. Version checks run first so a v2
        // manifest carrying *any* v3 feature surfaces the version error
        // rather than the (also true but less useful) shape mismatch.
        let is_mcp = self.plugin_type == PluginType::Mcp;
        let has_mcp_table = self.mcp.is_some();

        if has_mcp_table && self.manifest_version < MCP_MIN_MANIFEST_VERSION {
            return Err(format!(
                "[mcp] table requires manifest_version >= {} (got {})",
                MCP_MIN_MANIFEST_VERSION, self.manifest_version
            ));
        }

        if is_mcp && self.manifest_version < MCP_MIN_MANIFEST_VERSION {
            return Err(format!(
                "plugin_type = \"mcp\" requires manifest_version >= {} \
                 (got {}); bump the manifest to v3",
                MCP_MIN_MANIFEST_VERSION, self.manifest_version
            ));
        }

        if has_mcp_table && !is_mcp {
            return Err(format!(
                "[mcp] table is only valid when plugin_type = \"mcp\" \
                 (got plugin_type = \"{}\")",
                self.plugin_type.as_str()
            ));
        }

        Ok(())
    }
}

/// Parse a single manifest file from disk, migrate it to the v2 in-memory
/// shape, and validate it.
pub fn parse_manifest_file(path: &Path) -> Result<PluginManifest, ManifestParseError> {
    let raw = std::fs::read_to_string(path).map_err(|e| ManifestParseError::Io {
        path: path.to_path_buf(),
        source: e,
    })?;
    let mut manifest: PluginManifest =
        toml::from_str(&raw).map_err(|e| ManifestParseError::Toml {
            path: path.to_path_buf(),
            source: e,
        })?;
    manifest.migrate_to_current_in_memory();
    manifest
        .validate_all()
        .map_err(|message| ManifestParseError::Validation {
            path: path.to_path_buf(),
            message,
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

    // ---------- v2 schema tests ----------

    /// A v1 manifest (no `manifest_version`, no protocols/hooks/skill_refs)
    /// must load fine and be upgraded in memory to the current shape (v3)
    /// with default protocols.
    #[test]
    fn test_v1_manifest_loads_as_current() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("plugin-manifest.toml");
        std::fs::write(&path, SAMPLE).unwrap();

        let m = parse_manifest_file(&path).unwrap();

        assert_eq!(
            m.manifest_version, 3,
            "v1 must migrate to current (v3) in memory"
        );
        assert_eq!(m.protocols, vec!["openai_function".to_string()]);
        assert!(m.hooks.is_empty());
        assert!(m.skill_refs.is_empty());
        assert!(m.mcp.is_none(), "non-MCP migration leaves [mcp] absent");
    }

    #[test]
    fn test_v2_manifest_migrates_to_v3() {
        let raw = r#"
manifest_version = 2
name = "full"
version = "0.2.0"
plugin_type = "sync"
protocols = ["openai_function", "block"]
hooks = ["message.received", "session.patch"]
skill_refs = ["skill.core", "skill.search"]

[entry_point]
command = "python"
args = ["main.py"]
"#;
        let mut m: PluginManifest = toml::from_str(raw).unwrap();
        m.migrate_to_current_in_memory();
        m.validate_all().unwrap();

        assert_eq!(m.manifest_version, 3, "v2 migrates to v3 in memory");
        assert_eq!(m.protocols, vec!["openai_function", "block"]);
        assert_eq!(m.hooks, vec!["message.received", "session.patch"]);
        assert_eq!(m.skill_refs, vec!["skill.core", "skill.search"]);
        assert!(m.mcp.is_none());

        // Round-trip: serialise the migrated v3 shape and re-parse — the
        // version stamp survives unchanged.
        let serialised = toml::to_string(&m).unwrap();
        let round: PluginManifest = toml::from_str(&serialised).unwrap();
        assert_eq!(round.manifest_version, 3);
        assert_eq!(round.protocols, m.protocols);
        assert_eq!(round.hooks, m.hooks);
        assert_eq!(round.skill_refs, m.skill_refs);
    }

    #[test]
    fn test_invalid_protocol_rejected() {
        let raw = r#"
manifest_version = 2
name = "bad"
version = "0.1.0"
plugin_type = "sync"
protocols = ["custom"]
[entry_point]
command = "true"
"#;
        let mut m: PluginManifest = toml::from_str(raw).unwrap();
        m.migrate_to_current_in_memory();
        let err = m.validate_all().unwrap_err();
        assert!(err.contains("unknown protocol"), "{err}");
    }

    #[test]
    fn test_future_version_warns() {
        // `tracing_test` is not a dev-dep, so per spec we just assert the
        // forward-compat error surface for `manifest_version = 99`.
        let raw = r#"
manifest_version = 99
name = "future"
version = "0.1.0"
plugin_type = "sync"
[entry_point]
command = "true"
"#;
        let mut m: PluginManifest = toml::from_str(raw).unwrap();
        m.migrate_to_current_in_memory();
        // migrate is a no-op for version >= 3, so 99 is preserved.
        assert_eq!(m.manifest_version, 99);
        let err = m.validate_all().unwrap_err();
        assert!(err.contains("not supported"), "{err}");
    }

    #[test]
    fn test_unknown_hook_is_warning_not_error() {
        let raw = r#"
manifest_version = 2
name = "hooky"
version = "0.1.0"
plugin_type = "sync"
hooks = ["message.weird"]
[entry_point]
command = "true"
"#;
        let mut m: PluginManifest = toml::from_str(raw).unwrap();
        m.migrate_to_current_in_memory();
        // Forward-compat: unknown hook names only emit a warning log.
        m.validate_all()
            .expect("unknown hooks must not fail validation");
        assert_eq!(m.hooks, vec!["message.weird"]);
    }

    // ---------- Smoke-load: representative channel plugin manifests ----------
    //
    // The qq and telegram channels are currently linked directly into the
    // gateway (not yet shipped as separate plugins), so there is no
    // `plugin-manifest.toml` on disk for them. These smoke tests author the
    // manifest shapes those plugins will use once externalised and confirm
    // they load cleanly under the v2 loader.

    fn write_and_load(dir: &Path, body: &str) -> PluginManifest {
        let path = dir.join(MANIFEST_FILENAME);
        std::fs::write(&path, body).unwrap();
        parse_manifest_file(&path).unwrap()
    }

    #[test]
    fn smoke_load_qq_manifest_migrates_to_current() {
        let tmp = tempfile::tempdir().unwrap();
        let body = r#"
name = "qq"
version = "0.1.0"
description = "QQ (OneBot v11) channel adapter"
plugin_type = "service"

[entry_point]
command = "corlinman-channel-qq"
"#;
        let m = write_and_load(tmp.path(), body);
        assert_eq!(m.name, "qq");
        assert_eq!(m.manifest_version, 3);
        assert_eq!(m.protocols, vec!["openai_function".to_string()]);
    }

    #[test]
    fn smoke_load_telegram_manifest_migrates_to_current() {
        let tmp = tempfile::tempdir().unwrap();
        let body = r#"
name = "telegram"
version = "0.1.0"
description = "Telegram Bot API channel adapter"
plugin_type = "service"

[entry_point]
command = "corlinman-channel-telegram"
"#;
        let m = write_and_load(tmp.path(), body);
        assert_eq!(m.name, "telegram");
        assert_eq!(m.manifest_version, 3);
        assert_eq!(m.protocols, vec!["openai_function".to_string()]);
    }

    // ---------- v3 / MCP schema tests ----------

    /// `plugin_type = "mcp"` parses cleanly under v3 with an empty `[mcp]`
    /// table; v2 declaring the same combo is rejected with a version-bump
    /// hint.
    #[test]
    fn mcp_kind_parses_under_v3() {
        let v3 = r#"
manifest_version = 3
name = "fs"
version = "0.1.0"
plugin_type = "mcp"

[entry_point]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/data"]

[mcp]
"#;
        let mut m: PluginManifest = toml::from_str(v3).unwrap();
        m.migrate_to_current_in_memory();
        m.validate_all().expect("v3 mcp manifest must validate");
        assert_eq!(m.plugin_type, PluginType::Mcp);
        assert_eq!(m.plugin_type.as_str(), "mcp");
        assert!(m.mcp.is_some(), "[mcp] table parses into Some");

        let v2 = r#"
manifest_version = 2
name = "fs"
version = "0.1.0"
plugin_type = "mcp"
[entry_point]
command = "npx"
"#;
        let mut m: PluginManifest = toml::from_str(v2).unwrap();
        m.migrate_to_current_in_memory();
        // migrate is a no-op for explicit v2; the cross-field check fires.
        let err = m.validate_all().unwrap_err();
        assert!(
            err.contains("manifest_version >= 3") && err.contains("mcp"),
            "expected version-bump hint, got: {err}"
        );
    }

    /// Authoring an `[mcp]` table with an unknown field is a hard parse
    /// error (deny_unknown_fields), not a silent ignore.
    #[test]
    fn mcp_table_unknown_field_rejected() {
        let raw = r#"
manifest_version = 3
name = "fs"
version = "0.1.0"
plugin_type = "mcp"
[entry_point]
command = "npx"
[mcp]
mystery = 1
"#;
        let err = toml::from_str::<PluginManifest>(raw).unwrap_err();
        assert!(err.to_string().contains("unknown field"), "{err}");
    }

    /// Default `tools_allowlist` is fail-closed: `mode = "allow"` with an
    /// empty `names` list means zero tools exported.
    #[test]
    fn tools_allowlist_default_is_fail_closed() {
        let raw = r#"
manifest_version = 3
name = "fs"
version = "0.1.0"
plugin_type = "mcp"
[entry_point]
command = "npx"
[mcp]
"#;
        let mut m: PluginManifest = toml::from_str(raw).unwrap();
        m.migrate_to_current_in_memory();
        m.validate_all().unwrap();
        let mcp = m.mcp.as_ref().unwrap();
        assert_eq!(
            mcp.tools_allowlist.mode,
            AllowlistMode::Allow,
            "default mode is `allow`"
        );
        assert!(
            mcp.tools_allowlist.names.is_empty(),
            "default `names` is empty so allow-mode exports nothing"
        );
    }

    /// A v2 manifest carrying an `[mcp]` table is rejected — even when the
    /// plugin_type stays `sync`/`service`. Defends against operators
    /// authoring v3 fields without bumping the version stamp.
    #[test]
    fn v3_only_field_on_v2_manifest_rejected() {
        let raw = r#"
manifest_version = 2
name = "x"
version = "0.1.0"
plugin_type = "service"
[entry_point]
command = "true"
[mcp]
"#;
        let mut m: PluginManifest = toml::from_str(raw).unwrap();
        m.migrate_to_current_in_memory();
        let err = m.validate_all().unwrap_err();
        assert!(
            err.contains("[mcp]") && err.contains("manifest_version >= 3"),
            "expected v3 hint, got: {err}"
        );
    }

    /// An `[mcp]` table on a non-MCP plugin_type is a configuration error.
    #[test]
    fn mcp_table_on_non_mcp_plugin_rejected() {
        let raw = r#"
manifest_version = 3
name = "x"
version = "0.1.0"
plugin_type = "service"
[entry_point]
command = "true"
[mcp]
"#;
        let mut m: PluginManifest = toml::from_str(raw).unwrap();
        m.migrate_to_current_in_memory();
        let err = m.validate_all().unwrap_err();
        assert!(
            err.contains("plugin_type = \"mcp\"") && err.contains("service"),
            "expected mismatch hint, got: {err}"
        );
    }

    /// `RestartPolicy` defaults to `on_crash` and round-trips through TOML.
    #[test]
    fn mcp_restart_policy_defaults_and_parses() {
        assert_eq!(RestartPolicy::default(), RestartPolicy::OnCrash);

        let raw = r#"
manifest_version = 3
name = "fs"
version = "0.1.0"
plugin_type = "mcp"
[entry_point]
command = "npx"
[mcp]
restart_policy = "always"
crash_loop_max = 7
crash_loop_window_secs = 90
handshake_timeout_ms = 1234
idle_shutdown_secs = 30
autostart = true
"#;
        let mut m: PluginManifest = toml::from_str(raw).unwrap();
        m.migrate_to_current_in_memory();
        m.validate_all().unwrap();
        let mcp = m.mcp.as_ref().unwrap();
        assert!(mcp.autostart);
        assert_eq!(mcp.restart_policy, RestartPolicy::Always);
        assert_eq!(mcp.crash_loop_max, 7);
        assert_eq!(mcp.crash_loop_window_secs, 90);
        assert_eq!(mcp.handshake_timeout_ms, 1234);
        assert_eq!(mcp.idle_shutdown_secs, 30);
    }

    /// Empty `[mcp]` block uses the documented defaults (lazy, on_crash,
    /// 3 / 60s, 5000ms, 0s idle, no env, fail-closed allowlists).
    #[test]
    fn mcp_defaults_applied_when_table_empty() {
        let raw = r#"
manifest_version = 3
name = "fs"
version = "0.1.0"
plugin_type = "mcp"
[entry_point]
command = "npx"
[mcp]
"#;
        let mut m: PluginManifest = toml::from_str(raw).unwrap();
        m.migrate_to_current_in_memory();
        m.validate_all().unwrap();
        let mcp = m.mcp.as_ref().unwrap();
        assert!(!mcp.autostart);
        assert_eq!(mcp.restart_policy, RestartPolicy::OnCrash);
        assert_eq!(mcp.crash_loop_max, 3);
        assert_eq!(mcp.crash_loop_window_secs, 60);
        assert_eq!(mcp.handshake_timeout_ms, 5_000);
        assert_eq!(mcp.idle_shutdown_secs, 0);
        assert!(mcp.env_passthrough.allow.is_empty());
        assert!(mcp.env_passthrough.deny.is_empty());
        assert_eq!(mcp.tools_allowlist.mode, AllowlistMode::Allow);
        assert!(mcp.tools_allowlist.names.is_empty());
        assert_eq!(mcp.resources_allowlist.mode, AllowlistMode::Allow);
        assert!(mcp.resources_allowlist.patterns.is_empty());
    }

    /// `migrate_to_current_in_memory` is idempotent: running it twice on a
    /// v1 manifest yields the same v3 result as running it once.
    #[test]
    fn migrate_is_idempotent() {
        let mut m: PluginManifest = toml::from_str(SAMPLE).unwrap();
        m.migrate_to_current_in_memory();
        let v1 = m.manifest_version;
        m.migrate_to_current_in_memory();
        assert_eq!(m.manifest_version, v1);
        assert_eq!(m.manifest_version, 3);
    }

    // ---------- Iter 9: v2→v3 migration polish ----------
    //
    // The migration path is implemented in `migrate_to_current_in_memory`
    // (manifest.rs:459-484). Iter 9's job is to harden the surface
    // around it: backwards-compat aliases, repeated-load idempotency,
    // and the on-disk file invariant ("we never rewrite what's on
    // disk; the loader fills defaults in memory only").

    /// `parse_manifest_file` does NOT rewrite the manifest on disk
    /// when migrating an old version. The on-disk byte stream is the
    /// operator's source of truth; the gateway lifts to v3 only in
    /// memory.
    #[test]
    fn parse_manifest_file_does_not_rewrite_v1_on_disk() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join(MANIFEST_FILENAME);
        // Author a v1 manifest (no manifest_version stamp at all).
        std::fs::write(&path, SAMPLE).unwrap();
        let on_disk_before = std::fs::read_to_string(&path).unwrap();

        let m = parse_manifest_file(&path).unwrap();
        assert_eq!(m.manifest_version, 3, "v1 must lift to v3 in memory");

        let on_disk_after = std::fs::read_to_string(&path).unwrap();
        assert_eq!(
            on_disk_before, on_disk_after,
            "loader must NEVER touch the on-disk manifest"
        );
    }

    /// The v2 manifest in `test_v2_manifest_migrates_to_v3` (above)
    /// also writes nothing on disk — sanity check for the v2 case
    /// specifically; the v1 case is covered by the test above.
    #[test]
    fn parse_manifest_file_does_not_rewrite_v2_on_disk() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join(MANIFEST_FILENAME);
        let body = r#"
manifest_version = 2
name = "preserve"
version = "0.1.0"
plugin_type = "service"
protocols = ["openai_function"]

[entry_point]
command = "true"
"#;
        std::fs::write(&path, body).unwrap();
        let on_disk_before = std::fs::read_to_string(&path).unwrap();

        let m = parse_manifest_file(&path).unwrap();
        assert_eq!(m.manifest_version, 3);

        let on_disk_after = std::fs::read_to_string(&path).unwrap();
        assert_eq!(on_disk_before, on_disk_after);
    }

    /// Repeated loads of the same on-disk manifest yield byte-stable
    /// in-memory results — defends against accidentally introducing
    /// a non-deterministic default value (e.g. a HashMap-iteration
    /// derived field) into the migration.
    #[test]
    fn parse_manifest_file_repeated_loads_are_byte_stable() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join(MANIFEST_FILENAME);
        std::fs::write(&path, SAMPLE).unwrap();
        let m1 = parse_manifest_file(&path).unwrap();
        let m2 = parse_manifest_file(&path).unwrap();
        let s1 = toml::to_string(&m1).unwrap();
        let s2 = toml::to_string(&m2).unwrap();
        assert_eq!(s1, s2, "repeated loads must produce identical TOML");
    }

    /// `migrate_to_v2_in_memory` is the deprecated alias for
    /// `migrate_to_current_in_memory`; it must do exactly the same
    /// thing so any external caller still on the old name keeps
    /// working until the deprecation period ends.
    #[test]
    #[allow(deprecated)]
    fn deprecated_v2_migrate_alias_routes_to_current() {
        let mut a: PluginManifest = toml::from_str(SAMPLE).unwrap();
        let mut b: PluginManifest = toml::from_str(SAMPLE).unwrap();

        a.migrate_to_v2_in_memory(); // deprecated path
        b.migrate_to_current_in_memory();

        // Same version stamp.
        assert_eq!(a.manifest_version, b.manifest_version);
        assert_eq!(a.manifest_version, 3);
        // Same field-by-field shape after migration.
        assert_eq!(toml::to_string(&a).unwrap(), toml::to_string(&b).unwrap());
    }

    /// A v1 manifest declaring `plugin_type = "mcp"` gets the
    /// version-bump-required error — the migration deliberately
    /// leaves MCP-flavoured manifests at their authored version so
    /// the validator surfaces the error, rather than silently lifting
    /// them to v3 (which would mask the operator's typo).
    #[test]
    fn v1_mcp_flavoured_manifest_keeps_version_for_validation_error() {
        // No `manifest_version` stamp = v1 (default = 1).
        let raw = r#"
name = "fs"
version = "0.1.0"
plugin_type = "mcp"
[entry_point]
command = "npx"
"#;
        let mut m: PluginManifest = toml::from_str(raw).unwrap();
        m.migrate_to_current_in_memory();
        // The migration's v1->v2 step ran (default version is 1, so
        // the first if-block bumped to 2); the v2->v3 step short-
        // circuits because plugin_type = "mcp" is the MCP-flavoured
        // gate. So the in-memory version is 2, not 3.
        assert_eq!(
            m.manifest_version, 2,
            "MCP-flavoured manifests stop migrating at v2 so validation fires"
        );
        let err = m.validate_all().unwrap_err();
        assert!(
            err.contains("manifest_version >= 3"),
            "expected version-bump hint, got: {err}"
        );
    }

    /// A v3 manifest survives the migration unchanged: no version
    /// bump, defaults left untouched, on-disk shape == in-memory
    /// shape after a serialise round-trip.
    #[test]
    fn v3_manifest_round_trip_through_migration_is_noop() {
        let raw = r#"
manifest_version = 3
name = "fs"
version = "0.1.0"
plugin_type = "mcp"

[entry_point]
command = "npx"

[mcp]
autostart = true
"#;
        let mut a: PluginManifest = toml::from_str(raw).unwrap();
        a.migrate_to_current_in_memory();
        // Run migrate three more times — must be a no-op.
        a.migrate_to_current_in_memory();
        a.migrate_to_current_in_memory();
        a.migrate_to_current_in_memory();
        a.validate_all().unwrap();
        assert_eq!(a.manifest_version, 3);
        assert!(a.mcp.as_ref().unwrap().autostart);
    }

    /// End-to-end: write a v2 file on disk, load it, observe v3 in
    /// memory, verify on-disk file unchanged. Mirrors design test
    /// `v2_to_v3_migration_round_trip` from the test matrix.
    #[test]
    fn v2_to_v3_migration_round_trip_e2e() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join(MANIFEST_FILENAME);
        let body = r#"
manifest_version = 2
name = "rt"
version = "0.1.0"
plugin_type = "service"
protocols = ["openai_function", "block"]
hooks = ["message.received"]
skill_refs = ["skill.alpha"]

[entry_point]
command = "corlinman-channel-rt"

[capabilities]
disable_model_invocation = false
"#;
        std::fs::write(&path, body).unwrap();
        let on_disk_v2 = std::fs::read_to_string(&path).unwrap();

        let m = parse_manifest_file(&path).unwrap();
        assert_eq!(m.manifest_version, 3, "in-memory must be v3");
        assert_eq!(m.protocols, vec!["openai_function", "block"]);
        assert_eq!(m.hooks, vec!["message.received"]);
        assert_eq!(m.skill_refs, vec!["skill.alpha"]);
        assert!(m.mcp.is_none(), "v2 service manifest never gains [mcp]");

        let on_disk_after = std::fs::read_to_string(&path).unwrap();
        assert_eq!(
            on_disk_v2, on_disk_after,
            "the on-disk byte stream is the operator's source of truth"
        );
    }
}
