//! `McpAdapter` — owns one MCP child + client per registered MCP plugin.
//!
//! Iter 4 scope: spawn → `initialize` handshake → state-machine bookkeeping.
//! Tools list / multiplexed `tools/call` land in iter 5; crash-restart
//! in iter 6.
//!
//! State machine (design §Lifecycle):
//!
//! ```text
//!     Idle ── start() ──▶ Spawning ── initialize ──▶ Initialized
//!                            │                            │
//!                            └─ timeout / spawn err ──▶ Failed
//!                                                         │
//!                                                  stop() │
//!                                                         ▼
//!                                                      Stopped
//! ```
//!
//! `Initialized` is the terminal happy state for iter 4 — it advances
//! to `Healthy` once iter 5 lands `tools/list`. We deliberately keep
//! the state names design-aligned even when the layered behaviour
//! isn't all here yet.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use crate::runtime::mcp::schema::{
    tools as mcp_tools, ClientCapabilities, Implementation, InitializeParams, InitializeResult,
    MCP_PROTOCOL_VERSION,
};
use serde_json::Value as JsonValue;
use thiserror::Error;
use tokio::sync::RwLock;

use crate::manifest::{McpConfig, PluginManifest, PluginType};
use crate::runtime::mcp::client::{ClientError, McpStdioClient};
use crate::runtime::mcp::redact::{apply_env_passthrough, RedactError};
use crate::runtime::mcp_stdio::{build_child_env, SpawnError};

/// Phase of the per-plugin lifecycle. Cheap-to-clone enum the admin
/// surface (later iter) reads to render `/admin/plugins/:name`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AdapterStatus {
    /// Registered but not yet spawned (`autostart = false`, never called).
    Idle,
    /// Child spawned; handshake in flight.
    Spawning,
    /// Handshake complete; ready to take calls. (Iter 5 will rename
    /// the post-`tools/list` phase to `Healthy`.)
    Initialized,
    /// Child exited or admin asked us to stop.
    Stopped,
    /// Spawn / handshake / restart-loop failure. The string is human-
    /// readable cause for ops dashboards.
    Failed(String),
}

impl AdapterStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Idle => "idle",
            Self::Spawning => "spawning",
            Self::Initialized => "initialized",
            Self::Stopped => "stopped",
            Self::Failed(_) => "failed",
        }
    }
}

/// Lifecycle / dispatch errors surfaced by the adapter.
#[derive(Debug, Error)]
pub enum AdapterError {
    /// The plugin name isn't registered with the adapter.
    #[error("no MCP plugin named {0:?}")]
    UnknownPlugin(String),

    /// The plugin manifest is not `plugin_type = "mcp"`.
    #[error("plugin {0:?} is not an MCP plugin")]
    NotMcpPlugin(String),

    /// The manifest is missing the `[mcp]` table (validation oversight
    /// — should never reach here once `validate_all` ran).
    #[error("plugin {0:?} is missing [mcp] config")]
    MissingMcpConfig(String),

    /// `cwd` lookup / spawn failed.
    #[error("spawn failed for {plugin}: {source}")]
    Spawn {
        plugin: String,
        #[source]
        source: SpawnError,
    },

    /// Env-passthrough policy was malformed (bad glob).
    #[error("env policy error for {plugin}: {source}")]
    EnvPolicy {
        plugin: String,
        #[source]
        source: RedactError,
    },

    /// `initialize` handshake failed within `handshake_timeout_ms`.
    #[error("handshake failed for {plugin}: {source}")]
    Handshake {
        plugin: String,
        #[source]
        source: ClientError,
    },

    /// The MCP server returned an unexpected initialize result shape
    /// (e.g. wrong protocolVersion field type).
    #[error("invalid initialize result from {plugin}: {message}")]
    InvalidInitResult { plugin: String, message: String },
}

/// Per-plugin runtime state held by the adapter.
struct PluginSlot {
    manifest: Arc<PluginManifest>,
    /// Working directory to chdir the child into. Always the manifest dir.
    cwd: PathBuf,
    status: AdapterStatus,
    client: Option<McpStdioClient>,
    /// Negotiated protocolVersion + serverInfo. Logged once; not yet
    /// surfaced through admin (admin work is iter 9).
    server_info: Option<InitializeResult>,
}

impl PluginSlot {
    fn mcp_cfg(&self) -> Result<&McpConfig, AdapterError> {
        self.manifest
            .mcp
            .as_ref()
            .ok_or_else(|| AdapterError::MissingMcpConfig(self.manifest.name.clone()))
    }
}

/// Thread-safe registry of MCP plugins keyed by manifest name.
///
/// One adapter is shared across the gateway via `Arc<McpAdapter>`.
/// The registry is internally a `RwLock<HashMap>` rather than a
/// `DashMap` — the entry surface is small (a few dozen plugins per
/// gateway) and the read path threads through async locks already.
pub struct McpAdapter {
    slots: RwLock<HashMap<String, PluginSlot>>,
    /// `clientInfo` we advertise upstream during `initialize`.
    /// Static across the gateway lifetime.
    client_info: Implementation,
}

impl Default for McpAdapter {
    fn default() -> Self {
        Self::new()
    }
}

impl std::fmt::Debug for McpAdapter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("McpAdapter")
            .field("client_info", &self.client_info)
            .finish_non_exhaustive()
    }
}

impl McpAdapter {
    /// Build an adapter advertising `corlinman-plugins` as the client
    /// implementation name. Version mirrors the workspace crate version.
    pub fn new() -> Self {
        Self {
            slots: RwLock::new(HashMap::new()),
            client_info: Implementation {
                name: "corlinman-plugins".into(),
                version: env!("CARGO_PKG_VERSION").into(),
            },
        }
    }

    /// Register a manifest with the adapter without spawning. Used by
    /// the gateway boot path: walk the registry, register every MCP
    /// plugin, optionally `start_one` for those with `autostart = true`.
    pub async fn register(
        &self,
        manifest: Arc<PluginManifest>,
        cwd: PathBuf,
    ) -> Result<(), AdapterError> {
        if manifest.plugin_type != PluginType::Mcp {
            return Err(AdapterError::NotMcpPlugin(manifest.name.clone()));
        }
        if manifest.mcp.is_none() {
            return Err(AdapterError::MissingMcpConfig(manifest.name.clone()));
        }
        let name = manifest.name.clone();
        let slot = PluginSlot {
            manifest,
            cwd,
            status: AdapterStatus::Idle,
            client: None,
            server_info: None,
        };
        self.slots.write().await.insert(name, slot);
        Ok(())
    }

    /// Snapshot the current status of `name`, or [`AdapterError::UnknownPlugin`]
    /// if the manifest hasn't been registered.
    pub async fn status(&self, name: &str) -> Result<AdapterStatus, AdapterError> {
        let g = self.slots.read().await;
        g.get(name)
            .map(|s| s.status.clone())
            .ok_or_else(|| AdapterError::UnknownPlugin(name.to_string()))
    }

    /// Cheap probe used by tests / admin: is the spawned client still
    /// reachable (mpsc-open + reader hasn't observed EOF)?
    pub async fn is_alive(&self, name: &str) -> Result<bool, AdapterError> {
        let g = self.slots.read().await;
        let slot = g
            .get(name)
            .ok_or_else(|| AdapterError::UnknownPlugin(name.to_string()))?;
        Ok(slot.client.as_ref().map(|c| c.is_alive()).unwrap_or(false))
    }

    /// Snapshot every registered plugin's status. Order is by name.
    pub async fn statuses(&self) -> Vec<(String, AdapterStatus)> {
        let g = self.slots.read().await;
        let mut out: Vec<(String, AdapterStatus)> = g
            .iter()
            .map(|(k, v)| (k.clone(), v.status.clone()))
            .collect();
        out.sort_by(|a, b| a.0.cmp(&b.0));
        out
    }

    /// Spawn the child for `name` and run the MCP `initialize`
    /// handshake under `mcp.handshake_timeout_ms`. Idempotent: if the
    /// plugin is already `Initialized` the call is a no-op.
    pub async fn start_one(&self, name: &str) -> Result<(), AdapterError> {
        // Take everything we need under the lock, then drop the lock
        // before any await on child I/O. We can't hold the RwLock
        // write-guard across `client.call(...)` — that would
        // serialise the whole adapter behind one slow handshake.
        let (manifest, cwd, mcp_cfg, env_policy_allow_deny) = {
            let g = self.slots.read().await;
            let slot = g
                .get(name)
                .ok_or_else(|| AdapterError::UnknownPlugin(name.to_string()))?;
            if slot.status == AdapterStatus::Initialized {
                return Ok(());
            }
            let cfg = slot.mcp_cfg()?.clone();
            (
                Arc::clone(&slot.manifest),
                slot.cwd.clone(),
                cfg.clone(),
                cfg.env_passthrough.clone(),
            )
        };

        // Mark Spawning. A later concurrent start_one for the same
        // plugin will see Spawning and refuse — single-flight.
        {
            let mut g = self.slots.write().await;
            let slot = g
                .get_mut(name)
                .ok_or_else(|| AdapterError::UnknownPlugin(name.to_string()))?;
            if matches!(slot.status, AdapterStatus::Spawning) {
                return Ok(());
            }
            slot.status = AdapterStatus::Spawning;
        }

        let result = self
            .spawn_and_handshake(&manifest, &cwd, &mcp_cfg, &env_policy_allow_deny)
            .await;

        match result {
            Ok((client, init_result)) => {
                let mut g = self.slots.write().await;
                if let Some(slot) = g.get_mut(name) {
                    slot.client = Some(client);
                    slot.server_info = Some(init_result);
                    slot.status = AdapterStatus::Initialized;
                }
                Ok(())
            }
            Err(err) => {
                let msg = err.to_string();
                let mut g = self.slots.write().await;
                if let Some(slot) = g.get_mut(name) {
                    slot.client = None;
                    slot.server_info = None;
                    slot.status = AdapterStatus::Failed(msg);
                }
                Err(err)
            }
        }
    }

    /// Stop the child (if running) and mark the slot `Stopped`. Used
    /// by gateway shutdown and admin disable.
    pub async fn stop_one(&self, name: &str) -> Result<(), AdapterError> {
        let mut g = self.slots.write().await;
        let slot = g
            .get_mut(name)
            .ok_or_else(|| AdapterError::UnknownPlugin(name.to_string()))?;
        // Dropping the client triggers worker abort + kill_on_drop.
        slot.client = None;
        slot.server_info = None;
        slot.status = AdapterStatus::Stopped;
        Ok(())
    }

    /// Internal: do the spawn + initialize + tools/list pipeline.
    /// Iter 4 stops at initialize; iter 5 layers tools/list on top.
    async fn spawn_and_handshake(
        &self,
        manifest: &PluginManifest,
        cwd: &std::path::Path,
        mcp_cfg: &McpConfig,
        env_policy: &crate::manifest::EnvPassthrough,
    ) -> Result<(McpStdioClient, InitializeResult), AdapterError> {
        // 1. Resolve env passthrough against the parent env.
        let applied = apply_env_passthrough(env_policy, |k| std::env::var(k).ok())
            .map_err(|e| AdapterError::EnvPolicy {
                plugin: manifest.name.clone(),
                source: e,
            })?;
        let env: Vec<(std::ffi::OsString, std::ffi::OsString)> =
            build_child_env(applied.forwarded.into_iter().map(|(k, v)| (k, v)));

        // 2. Spawn the child + wire the framing layer.
        let client = McpStdioClient::connect_stdio(
            &manifest.entry_point.command,
            &manifest.entry_point.args,
            cwd,
            env,
        )
        .map_err(|e| match e {
            ClientError::Spawn(s) => AdapterError::Spawn {
                plugin: manifest.name.clone(),
                source: s,
            },
            other => AdapterError::Handshake {
                plugin: manifest.name.clone(),
                source: other,
            },
        })?;

        // 3. Send `initialize`.
        let init_params = InitializeParams {
            protocol_version: MCP_PROTOCOL_VERSION.into(),
            capabilities: ClientCapabilities::default(),
            client_info: self.client_info.clone(),
        };
        let params_json = serde_json::to_value(&init_params).map_err(|e| {
            AdapterError::Handshake {
                plugin: manifest.name.clone(),
                source: ClientError::Serde(e),
            }
        })?;
        let deadline = Duration::from_millis(mcp_cfg.handshake_timeout_ms);
        let raw = client
            .call("initialize", params_json, Some(deadline))
            .await
            .map_err(|e| AdapterError::Handshake {
                plugin: manifest.name.clone(),
                source: e,
            })?;

        // 4. Parse the result.
        let init_result: InitializeResult = serde_json::from_value(raw).map_err(|e| {
            AdapterError::InvalidInitResult {
                plugin: manifest.name.clone(),
                message: format!("could not deserialize InitializeResult: {e}"),
            }
        })?;

        // 5. Send the `notifications/initialized` notification per
        //    MCP spec — most servers idle until they see it.
        if let Err(e) = client
            .notify("notifications/initialized", JsonValue::Object(Default::default()))
            .await
        {
            // Notification failure means the writer task exited; the
            // child is gone. Surface as handshake error so the slot
            // ends up Failed rather than half-up.
            return Err(AdapterError::Handshake {
                plugin: manifest.name.clone(),
                source: e,
            });
        }

        tracing::info!(
            plugin = manifest.name,
            server = %init_result.server_info.name,
            version = %init_result.server_info.version,
            protocol = %init_result.protocol_version,
            "MCP initialize handshake complete",
        );

        Ok((client, init_result))
    }

    // ---- Iter 5 surface (lands in next commit) ----
    // Public skeleton placed here so iter 5 can fill the body without
    // a circular-import shuffle.

    /// Internal helper used by iter 5+: fetch the live client for
    /// `name`, returning `UnknownPlugin` / states without one.
    pub(crate) async fn live_client(&self, name: &str) -> Result<McpStdioClient, AdapterError> {
        let g = self.slots.read().await;
        let slot = g
            .get(name)
            .ok_or_else(|| AdapterError::UnknownPlugin(name.to_string()))?;
        slot.client
            .clone()
            .ok_or_else(|| match &slot.status {
                AdapterStatus::Failed(reason) => AdapterError::Handshake {
                    plugin: name.to_string(),
                    source: ClientError::Disconnected(reason.clone()),
                },
                _ => AdapterError::Handshake {
                    plugin: name.to_string(),
                    source: ClientError::Disconnected(format!(
                        "no live client (status={})",
                        slot.status.as_str()
                    )),
                },
            })
    }

    /// Lookup the manifest's `McpConfig` by name. Used by iter 5 to
    /// get the `tools_allowlist` at call time.
    pub(crate) async fn mcp_config(&self, name: &str) -> Result<McpConfig, AdapterError> {
        let g = self.slots.read().await;
        let slot = g
            .get(name)
            .ok_or_else(|| AdapterError::UnknownPlugin(name.to_string()))?;
        slot.mcp_cfg().cloned()
    }

    /// Touch — used by iter 5 to import the schema types alongside
    /// the adapter without an "unused import" warning until the
    /// implementation calls them.
    #[doc(hidden)]
    pub fn _touch_iter5(_: mcp_tools::ListResult) {}
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::manifest::{
        AllowlistMode, EntryPoint, EnvPassthrough, McpConfig, PluginManifest, PluginType,
        ResourcesAllowlist, RestartPolicy, ToolsAllowlist,
    };

    /// Construct a minimal MCP manifest. `command`/`args` choose the
    /// child binary; `handshake_timeout_ms` lets tests dial the
    /// initialize deadline up or down.
    fn manifest(name: &str, command: &str, args: &[&str], handshake_ms: u64) -> Arc<PluginManifest> {
        Arc::new(PluginManifest {
            manifest_version: 3,
            name: name.into(),
            version: "0.1.0".into(),
            description: String::new(),
            author: String::new(),
            plugin_type: PluginType::Mcp,
            entry_point: EntryPoint {
                command: command.into(),
                args: args.iter().map(|s| s.to_string()).collect(),
                env: Default::default(),
            },
            communication: Default::default(),
            capabilities: Default::default(),
            sandbox: Default::default(),
            mcp: Some(McpConfig {
                autostart: false,
                restart_policy: RestartPolicy::OnCrash,
                crash_loop_max: 3,
                crash_loop_window_secs: 60,
                handshake_timeout_ms: handshake_ms,
                idle_shutdown_secs: 0,
                env_passthrough: EnvPassthrough {
                    allow: vec![],
                    deny: vec![],
                },
                tools_allowlist: ToolsAllowlist {
                    mode: AllowlistMode::All,
                    names: vec![],
                },
                resources_allowlist: ResourcesAllowlist::default(),
            }),
            meta: None,
            protocols: vec!["openai_function".into()],
            hooks: vec![],
            skill_refs: vec![],
        })
    }

    /// Spawn a sh-piped MCP server that responds to `initialize`
    /// with a well-formed `InitializeResult`. Writes go to a temp
    /// dir we leak deliberately so the cwd outlives the child.
    fn awk_initialize_responder() -> (&'static str, Vec<String>) {
        // Returns (cmd, args) — cwd is the test-supplied tempdir.
        // The awk script:
        //   - reads each newline-delimited request,
        //   - extracts the id (numeric or quoted),
        //   - emits a result frame whose result field is a hand-shaped
        //     InitializeResult.
        let script = r#"awk '
            {
                line=$0
                # Find "id":
                m = match(line, /"id":[ ]*[0-9]+/)
                if (m == 0) {
                    m = match(line, /"id":[ ]*"[^"]*"/)
                }
                if (m == 0) {
                    next
                }
                idstr = substr(line, RSTART+5, RLENGTH-5)
                gsub(/^[ ]+/, "", idstr)
                # Detect method to decide payload.
                if (line ~ /"method"[ ]*:[ ]*"initialize"/) {
                    printf "{\"jsonrpc\":\"2.0\",\"id\":%s,\"result\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{\"tools\":{}},\"serverInfo\":{\"name\":\"awk-mcp\",\"version\":\"0.0.1\"}}}\n", idstr
                    fflush()
                }
            }'"#;
        (
            "sh",
            vec!["-c".into(), script.into()],
        )
    }

    #[tokio::test]
    async fn handshake_happy_path() {
        if which::which("awk").is_err() || which::which("sh").is_err() {
            eprintln!("awk/sh not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_initialize_responder();
        let m = manifest("fs-test", cmd, &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(), 5_000);
        let adapter = McpAdapter::new();
        adapter.register(m.clone(), tmp.path().to_path_buf()).await.unwrap();
        assert_eq!(
            adapter.status("fs-test").await.unwrap(),
            AdapterStatus::Idle
        );

        adapter.start_one("fs-test").await.expect("handshake must succeed");

        assert_eq!(
            adapter.status("fs-test").await.unwrap(),
            AdapterStatus::Initialized
        );
        assert!(adapter.is_alive("fs-test").await.unwrap());

        // stop_one is clean.
        adapter.stop_one("fs-test").await.unwrap();
        assert_eq!(
            adapter.status("fs-test").await.unwrap(),
            AdapterStatus::Stopped
        );
        assert!(!adapter.is_alive("fs-test").await.unwrap());
    }

    /// Server that ignores `initialize` for longer than the handshake
    /// timeout → child is killed, slot transitions to `Failed`, status
    /// reads back the cause.
    #[tokio::test]
    async fn handshake_timeout_marks_failed() {
        if which::which("sleep").is_err() {
            eprintln!("sleep not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let m = manifest("ghost", "sleep", &["10"], 50); // 50ms handshake budget
        let adapter = McpAdapter::new();
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();

        let err = adapter.start_one("ghost").await.expect_err("must fail");
        assert!(
            matches!(err, AdapterError::Handshake { .. }),
            "expected Handshake, got {err:?}"
        );

        match adapter.status("ghost").await.unwrap() {
            AdapterStatus::Failed(reason) => {
                assert!(
                    reason.contains("timed out") || reason.contains("handshake"),
                    "expected timeout reason, got: {reason}"
                );
            }
            other => panic!("expected Failed, got {other:?}"),
        }
        // No live client.
        assert!(!adapter.is_alive("ghost").await.unwrap());
    }

    /// Spawning a binary that doesn't exist surfaces `AdapterError::Spawn`
    /// and parks the slot in `Failed`.
    #[tokio::test]
    async fn missing_binary_marks_failed() {
        let tmp = tempfile::tempdir().unwrap();
        let m = manifest(
            "ghost-bin",
            "/definitely/not/a/real/binary/c2-iter4",
            &[],
            5_000,
        );
        let adapter = McpAdapter::new();
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        let err = adapter
            .start_one("ghost-bin")
            .await
            .expect_err("missing binary must fail");
        assert!(
            matches!(err, AdapterError::Spawn { .. }),
            "expected Spawn, got {err:?}"
        );
        assert!(matches!(
            adapter.status("ghost-bin").await.unwrap(),
            AdapterStatus::Failed(_)
        ));
    }

    /// `register` rejects non-MCP plugins.
    #[tokio::test]
    async fn register_rejects_non_mcp_plugin() {
        let mut m = (*manifest("svc", "true", &[], 5_000)).clone();
        m.plugin_type = PluginType::Sync;
        m.mcp = None;
        let adapter = McpAdapter::new();
        let err = adapter
            .register(Arc::new(m), tempfile::tempdir().unwrap().path().to_path_buf())
            .await
            .expect_err("non-mcp must reject");
        assert!(matches!(err, AdapterError::NotMcpPlugin(_)));
    }

    /// `start_one` is idempotent: calling it a second time on a healthy
    /// slot is a no-op (no re-spawn, no extra handshake).
    #[tokio::test]
    async fn start_one_is_idempotent() {
        if which::which("awk").is_err() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_initialize_responder();
        let m = manifest("idem", cmd, &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(), 5_000);
        let adapter = McpAdapter::new();
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();

        adapter.start_one("idem").await.unwrap();
        let first_alive = adapter.is_alive("idem").await.unwrap();
        adapter.start_one("idem").await.unwrap();
        let second_alive = adapter.is_alive("idem").await.unwrap();
        assert!(first_alive && second_alive);
        assert_eq!(
            adapter.status("idem").await.unwrap(),
            AdapterStatus::Initialized
        );
    }

    /// `statuses` returns slots sorted by name and reflects the latest
    /// state.
    #[tokio::test]
    async fn statuses_listing_is_sorted_and_includes_failed() {
        let tmp = tempfile::tempdir().unwrap();
        let adapter = McpAdapter::new();
        let m1 = manifest(
            "zzz",
            "/definitely/not/a/real/binary/c2-iter4",
            &[],
            5_000,
        );
        let m2 = manifest(
            "aaa",
            "/definitely/not/a/real/binary/c2-iter4",
            &[],
            5_000,
        );
        adapter.register(m1, tmp.path().to_path_buf()).await.unwrap();
        adapter.register(m2, tmp.path().to_path_buf()).await.unwrap();

        let _ = adapter.start_one("aaa").await;
        let _ = adapter.start_one("zzz").await;

        let s = adapter.statuses().await;
        assert_eq!(s.len(), 2);
        assert_eq!(s[0].0, "aaa");
        assert_eq!(s[1].0, "zzz");
        assert!(matches!(s[0].1, AdapterStatus::Failed(_)));
        assert!(matches!(s[1].1, AdapterStatus::Failed(_)));
    }
}
