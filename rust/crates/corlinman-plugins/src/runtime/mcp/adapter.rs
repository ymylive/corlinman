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

use crate::manifest::{AllowlistMode, McpConfig, PluginManifest, PluginType, Tool, ToolsAllowlist};
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

    /// The MCP server returned an unexpected `tools/list` shape.
    #[error("invalid tools/list result from {plugin}: {message}")]
    InvalidToolsListResult { plugin: String, message: String },

    /// A `tools/call` failed — wire-level error (transport or server-side).
    #[error("tools/call failed for {plugin}.{tool}: {source}")]
    Call {
        plugin: String,
        tool: String,
        #[source]
        source: ClientError,
    },

    /// Caller asked for a tool the manifest's allowlist (or the
    /// upstream server) doesn't expose.
    #[error("tool {tool:?} not exposed by plugin {plugin:?}")]
    UnknownTool { plugin: String, tool: String },

    /// The plugin is administratively disabled (`.disabled` sentinel
    /// in the plugin's manifest dir, or admin invoked
    /// [`McpAdapter::disable_one`] this session). Iter 8.
    #[error("plugin {0:?} is administratively disabled")]
    Disabled(String),

    /// Persisting / removing the `.disabled` sentinel file failed.
    /// Iter 8.
    #[error("failed to persist disabled flag for {plugin:?}: {message}")]
    SentinelIo { plugin: String, message: String },
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
    /// Tools surface advertised to the rest of the gateway. Populated
    /// after a successful `tools/list` + allowlist filter. The
    /// `Tool::parameters` field is the upstream `inputSchema` JSON
    /// verbatim — corlinman's dispatcher already validates against
    /// JSON-Schema-draft-07, no shape conversion needed.
    resolved_tools: Vec<Tool>,
    /// Iter 8: when true, `start_one` is a no-op and `call_tool` rejects
    /// with `Disabled`. Persisted via a `.disabled` sentinel file in the
    /// plugin's manifest dir so the disabled state survives a gateway
    /// restart without a second config file.
    disabled: bool,
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
        // Honour any `.disabled` sentinel left by a prior admin
        // disable: the operator's intent must survive gateway restart.
        let disabled = sentinel_path(&cwd).exists();
        let slot = PluginSlot {
            manifest,
            cwd,
            status: AdapterStatus::Idle,
            client: None,
            server_info: None,
            resolved_tools: Vec::new(),
            disabled,
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
            if slot.disabled {
                // A disabled slot is a deliberate admin no-op: respect
                // the sentinel and return without spawning. The status
                // stays whatever it was (Idle on first register; Stopped
                // after a previous disable_one). This keeps `disable +
                // start` an idempotent fail-safe rather than a silent
                // spawn.
                return Err(AdapterError::Disabled(name.to_string()));
            }
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
            Ok((client, init_result, resolved_tools)) => {
                let mut g = self.slots.write().await;
                if let Some(slot) = g.get_mut(name) {
                    slot.client = Some(client);
                    slot.server_info = Some(init_result);
                    slot.resolved_tools = resolved_tools;
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
                    slot.resolved_tools.clear();
                    slot.status = AdapterStatus::Failed(msg);
                }
                Err(err)
            }
        }
    }

    /// Stop the child (if running) and mark the slot `Stopped`. Used
    /// by gateway shutdown and admin disable.
    ///
    /// Calls `McpStdioClient::shutdown` *before* dropping the slot's
    /// clone so the underlying tasks exit and the disconnect notify
    /// fires — the supervisor (iter 6) wakes from `wait_disconnect`
    /// and either respawns or terminates per its policy.
    /// Implementation note: the child kill goes through `shutdown`
    /// rather than relying on `Drop`'s "last clone" gate because
    /// concurrent clones (the supervisor's, an in-flight `call_tool`)
    /// would otherwise keep the inner Arc alive and the process
    /// would survive the slot transition.
    pub async fn stop_one(&self, name: &str) -> Result<(), AdapterError> {
        // Take the client out under the lock, then drop the lock
        // before awaiting `shutdown()` (no need to serialise the
        // adapter behind a teardown).
        let client = {
            let mut g = self.slots.write().await;
            let slot = g
                .get_mut(name)
                .ok_or_else(|| AdapterError::UnknownPlugin(name.to_string()))?;
            slot.server_info = None;
            slot.resolved_tools.clear();
            slot.status = AdapterStatus::Stopped;
            slot.client.take()
        };
        if let Some(c) = client {
            c.shutdown().await;
        }
        Ok(())
    }

    /// Iter 8: read-only check used by admin endpoints to render the
    /// "disabled" banner without forcing a spawn attempt.
    pub async fn is_disabled(&self, name: &str) -> Result<bool, AdapterError> {
        let g = self.slots.read().await;
        g.get(name)
            .map(|s| s.disabled)
            .ok_or_else(|| AdapterError::UnknownPlugin(name.to_string()))
    }

    /// Iter 8: administratively disable a plugin. Stops the child if
    /// running, flags the slot so `start_one` is a no-op, and writes
    /// a `.disabled` sentinel into the plugin's manifest dir so the
    /// state persists across gateway restart.
    ///
    /// Idempotent: calling `disable_one` on an already-disabled slot
    /// returns `Ok(())` without re-touching the filesystem.
    ///
    /// Sentinel-write failure surfaces as
    /// [`AdapterError::SentinelIo`]; the in-memory `disabled` flag
    /// is rolled back so admin can retry.
    pub async fn disable_one(&self, name: &str) -> Result<(), AdapterError> {
        let (already, cwd, client) = {
            let mut g = self.slots.write().await;
            let slot = g
                .get_mut(name)
                .ok_or_else(|| AdapterError::UnknownPlugin(name.to_string()))?;
            let already = slot.disabled;
            slot.disabled = true;
            slot.server_info = None;
            slot.resolved_tools.clear();
            // Mark Stopped so admin/status reflects "disable took the
            // child down". We deliberately don't introduce a separate
            // `Disabled` AdapterStatus variant — the `.disabled`
            // sentinel + `is_disabled()` is the single source of truth
            // and adding a status variant would force every consumer
            // to handle a fifth state.
            if !already {
                slot.status = AdapterStatus::Stopped;
            }
            (already, slot.cwd.clone(), slot.client.take())
        };

        if let Some(c) = client {
            c.shutdown().await;
        }

        if already {
            return Ok(());
        }

        let path = sentinel_path(&cwd);
        if let Err(err) = std::fs::write(&path, b"disabled\n") {
            // Roll back the in-memory flag so admin sees the
            // failure rather than a half-applied state.
            let mut g = self.slots.write().await;
            if let Some(slot) = g.get_mut(name) {
                slot.disabled = false;
            }
            return Err(AdapterError::SentinelIo {
                plugin: name.to_string(),
                message: format!("writing sentinel {}: {err}", path.display()),
            });
        }
        tracing::info!(plugin = name, sentinel = %path.display(), "MCP plugin disabled");
        Ok(())
    }

    /// Iter 8: re-enable a plugin previously disabled. Removes the
    /// `.disabled` sentinel file (best-effort: a missing file is fine)
    /// and clears the in-memory flag. Does NOT auto-spawn — admin must
    /// follow up with `start_one` (or rely on autostart-on-next-call).
    pub async fn enable_one(&self, name: &str) -> Result<(), AdapterError> {
        let cwd = {
            let mut g = self.slots.write().await;
            let slot = g
                .get_mut(name)
                .ok_or_else(|| AdapterError::UnknownPlugin(name.to_string()))?;
            if !slot.disabled {
                return Ok(());
            }
            slot.disabled = false;
            slot.cwd.clone()
        };

        let path = sentinel_path(&cwd);
        match std::fs::remove_file(&path) {
            Ok(()) => {}
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => {
                // Roll back: keep the slot disabled so the on-disk
                // sentinel and in-memory flag stay aligned. The admin
                // surface returns 500 and ops can retry.
                let mut g = self.slots.write().await;
                if let Some(slot) = g.get_mut(name) {
                    slot.disabled = true;
                }
                return Err(AdapterError::SentinelIo {
                    plugin: name.to_string(),
                    message: format!("removing sentinel {}: {err}", path.display()),
                });
            }
        }
        tracing::info!(plugin = name, "MCP plugin re-enabled");
        Ok(())
    }

    /// Iter 8: stop + start in one shot. Used by
    /// `POST /admin/plugins/:name/restart` so an operator can recover
    /// from a `Failed` slot without restarting the gateway. If the
    /// plugin is currently disabled the call returns
    /// [`AdapterError::Disabled`] without touching the child, matching
    /// `start_one`'s precedent.
    pub async fn restart_one(&self, name: &str) -> Result<(), AdapterError> {
        // Allow restart while currently Failed by clearing the slot's
        // in-memory state via stop_one first; start_one's
        // single-flight + Spawning gate handles the race against any
        // concurrent admin call.
        self.stop_one(name).await?;
        // After stop_one the slot is in `Stopped`; start_one does the
        // disabled check itself.
        self.start_one(name).await
    }

    /// Internal: do the spawn + initialize + tools/list pipeline.
    async fn spawn_and_handshake(
        &self,
        manifest: &PluginManifest,
        cwd: &std::path::Path,
        mcp_cfg: &McpConfig,
        env_policy: &crate::manifest::EnvPassthrough,
    ) -> Result<(McpStdioClient, InitializeResult, Vec<Tool>), AdapterError> {
        // 1. Resolve env passthrough against the parent env.
        let applied =
            apply_env_passthrough(env_policy, |k| std::env::var(k).ok()).map_err(|e| {
                AdapterError::EnvPolicy {
                    plugin: manifest.name.clone(),
                    source: e,
                }
            })?;
        let env: Vec<(std::ffi::OsString, std::ffi::OsString)> =
            build_child_env(applied.forwarded);

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
        let params_json =
            serde_json::to_value(&init_params).map_err(|e| AdapterError::Handshake {
                plugin: manifest.name.clone(),
                source: ClientError::Serde(e),
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
        let init_result: InitializeResult =
            serde_json::from_value(raw).map_err(|e| AdapterError::InvalidInitResult {
                plugin: manifest.name.clone(),
                message: format!("could not deserialize InitializeResult: {e}"),
            })?;

        // 5. Send the `notifications/initialized` notification per
        //    MCP spec — most servers idle until they see it.
        if let Err(e) = client
            .notify(
                "notifications/initialized",
                JsonValue::Object(Default::default()),
            )
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

        // 6. tools/list — required by every MCP server worth its salt.
        // We use the same handshake_timeout_ms budget; servers that
        // can't list tools within that window are effectively dead
        // (and the operator will see a Failed slot).
        let raw_list = client
            .call(
                "tools/list",
                JsonValue::Object(Default::default()),
                Some(deadline),
            )
            .await
            .map_err(|e| AdapterError::Handshake {
                plugin: manifest.name.clone(),
                source: e,
            })?;
        let list: mcp_tools::ListResult =
            serde_json::from_value(raw_list).map_err(|e| AdapterError::InvalidToolsListResult {
                plugin: manifest.name.clone(),
                message: format!("could not deserialize ListResult: {e}"),
            })?;

        let resolved = filter_and_project_tools(&list.tools, &mcp_cfg.tools_allowlist);
        tracing::info!(
            plugin = manifest.name,
            upstream_tools = list.tools.len(),
            exported_tools = resolved.len(),
            "MCP tools/list resolved",
        );

        Ok((client, init_result, resolved))
    }

    /// Internal: fetch the live MCP client for `name`, returning
    /// `Disconnected` semantics when the slot has none. Used by the
    /// crash-restart supervisor (iter 6) to park on the client's
    /// `wait_disconnect` future.
    pub(crate) async fn live_client_for_supervisor(
        &self,
        name: &str,
    ) -> Result<McpStdioClient, AdapterError> {
        let g = self.slots.read().await;
        let slot = g
            .get(name)
            .ok_or_else(|| AdapterError::UnknownPlugin(name.to_string()))?;
        slot.client.clone().ok_or_else(|| AdapterError::Handshake {
            plugin: name.to_string(),
            source: ClientError::Disconnected(format!(
                "no live client (status={})",
                slot.status.as_str()
            )),
        })
    }

    /// Snapshot of the resolved tool set for `name`. Empty if the
    /// plugin hasn't been started or `tools_allowlist` filtered them
    /// all out. Order matches `tools/list` ⨯ allowlist iteration.
    pub async fn tools_for(&self, name: &str) -> Result<Vec<Tool>, AdapterError> {
        let g = self.slots.read().await;
        let slot = g
            .get(name)
            .ok_or_else(|| AdapterError::UnknownPlugin(name.to_string()))?;
        Ok(slot.resolved_tools.clone())
    }

    /// Issue a `tools/call` against the plugin. The deadline defaults
    /// to `handshake_timeout_ms * 6` (the design says deadline_ms
    /// flows from the dispatcher; without one we err on the
    /// conservative side — six handshake budgets ≈ 30s for the
    /// default 5_000ms config).
    ///
    /// Returns the parsed `tools/call` `CallResult`. The dispatcher
    /// surfaces this as `PluginOutput::Success { content }` (json
    /// encoded) or `PluginOutput::Error { code, message }` if
    /// `is_error == true` — projection is iter 6's `PluginRuntime`
    /// trait impl, not the adapter's job.
    pub async fn call_tool(
        &self,
        name: &str,
        tool: &str,
        arguments: JsonValue,
        deadline: Option<Duration>,
    ) -> Result<mcp_tools::CallResult, AdapterError> {
        // 1. Allowlist check — fast path that doesn't touch the live
        // client, so a denied call doesn't queue behind a slow
        // legitimate one.
        let (client, deadline) = {
            let g = self.slots.read().await;
            let slot = g
                .get(name)
                .ok_or_else(|| AdapterError::UnknownPlugin(name.to_string()))?;
            if slot.disabled {
                return Err(AdapterError::Disabled(name.to_string()));
            }
            if !slot.resolved_tools.iter().any(|t| t.name == tool) {
                return Err(AdapterError::UnknownTool {
                    plugin: name.to_string(),
                    tool: tool.to_string(),
                });
            }
            let client = slot.client.clone().ok_or_else(|| {
                let reason = match &slot.status {
                    AdapterStatus::Failed(r) => r.clone(),
                    other => format!("status={}", other.as_str()),
                };
                AdapterError::Handshake {
                    plugin: name.to_string(),
                    source: ClientError::Disconnected(reason),
                }
            })?;
            let cfg = slot.mcp_cfg()?;
            let default_dl = Duration::from_millis(cfg.handshake_timeout_ms.saturating_mul(6));
            (client, deadline.unwrap_or(default_dl))
        };

        // 2. Send `tools/call`. The McpStdioClient's per-id correlation
        // is what gives us multiplex: many in-flight calls can share
        // one client, each parked on its own oneshot.
        let params = serde_json::json!({
            "name": tool,
            "arguments": arguments,
        });
        let raw = client
            .call("tools/call", params, Some(deadline))
            .await
            .map_err(|e| AdapterError::Call {
                plugin: name.to_string(),
                tool: tool.to_string(),
                source: e,
            })?;
        let result: mcp_tools::CallResult =
            serde_json::from_value(raw).map_err(|e| AdapterError::InvalidToolsListResult {
                plugin: name.to_string(),
                message: format!("could not deserialize CallResult for {tool}: {e}"),
            })?;
        Ok(result)
    }
}

/// Filename of the sentinel file used to persist the
/// administratively-disabled state of an MCP plugin. Iter 8.
///
/// Living in the plugin's manifest dir means the sentinel travels
/// with the manifest itself (a `git mv` of the dir keeps it
/// disabled; deleting the dir removes the disable along with the
/// plugin). The reloader (M6) watches the same directory, so a
/// future enhancement could let `touch .disabled` outside the
/// admin API toggle the state — for iter 8 we only require the
/// admin path to use it.
pub const DISABLED_SENTINEL_FILENAME: &str = ".disabled";

/// Resolve the sentinel path for a plugin given its `cwd`
/// (always the manifest directory; see `register`'s cwd argument).
fn sentinel_path(cwd: &std::path::Path) -> std::path::PathBuf {
    cwd.join(DISABLED_SENTINEL_FILENAME)
}

/// Apply a [`ToolsAllowlist`] to the upstream `tools/list` payload and
/// project each surviving descriptor into a corlinman [`Tool`].
///
/// Modes (mirrors `manifest::AllowlistMode`):
///   - `Allow`: emit only descriptors whose `name` is in
///     `allowlist.names`. Default. Empty `names` ⇒ zero exports
///     (fail-closed).
///   - `Deny`: emit every descriptor *except* those in
///     `allowlist.names`.
///   - `All`: emit every descriptor; ignore `names`.
fn filter_and_project_tools(
    upstream: &[mcp_tools::ToolDescriptor],
    allowlist: &ToolsAllowlist,
) -> Vec<Tool> {
    let mut out = Vec::with_capacity(upstream.len());
    for d in upstream {
        let allowed = match allowlist.mode {
            AllowlistMode::All => true,
            AllowlistMode::Allow => allowlist.names.iter().any(|n| n == &d.name),
            AllowlistMode::Deny => !allowlist.names.iter().any(|n| n == &d.name),
        };
        if !allowed {
            continue;
        }
        out.push(Tool {
            name: d.name.clone(),
            description: d.description.clone().unwrap_or_default(),
            parameters: d.input_schema.clone(),
        });
    }
    out
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
    /// initialize deadline up or down. `allowlist` defaults to `All`
    /// when callers pass `None`, matching the iter-4 test surface.
    fn manifest_with(
        name: &str,
        command: &str,
        args: &[&str],
        handshake_ms: u64,
        allowlist: Option<ToolsAllowlist>,
    ) -> Arc<PluginManifest> {
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
                tools_allowlist: allowlist.unwrap_or(ToolsAllowlist {
                    mode: AllowlistMode::All,
                    names: vec![],
                }),
                resources_allowlist: ResourcesAllowlist::default(),
            }),
            meta: None,
            protocols: vec!["openai_function".into()],
            hooks: vec![],
            skill_refs: vec![],
        })
    }

    fn manifest(
        name: &str,
        command: &str,
        args: &[&str],
        handshake_ms: u64,
    ) -> Arc<PluginManifest> {
        manifest_with(name, command, args, handshake_ms, None)
    }

    /// Spawn a sh-piped MCP server that responds to `initialize`,
    /// `tools/list`, and `tools/call` (returning a stable `echo`
    /// payload). Iter 4 only used initialize; iter 5 reuses this
    /// helper for the call-multiplex tests.
    ///
    /// The script:
    ///   - extracts the request id (numeric or quoted),
    ///   - branches on `method`,
    ///   - prints a well-formed result frame.
    fn awk_initialize_responder() -> (&'static str, Vec<String>) {
        let script = r#"awk '
            {
                line=$0
                m = match(line, /"id":[ ]*[0-9]+/)
                if (m == 0) {
                    m = match(line, /"id":[ ]*"[^"]*"/)
                }
                if (m == 0) {
                    next
                }
                idstr = substr(line, RSTART+5, RLENGTH-5)
                gsub(/^[ ]+/, "", idstr)
                # `idstr` is the verbatim wire id (e.g. `"req-2"` or
                # `42`); for embedding inside another quoted string
                # (the call-result text) we want it stripped of any
                # surrounding quotes — call that `idtxt`.
                idtxt = idstr
                gsub(/"/, "", idtxt)
                if (line ~ /"method"[ ]*:[ ]*"initialize"/) {
                    printf "{\"jsonrpc\":\"2.0\",\"id\":%s,\"result\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{\"tools\":{}},\"serverInfo\":{\"name\":\"awk-mcp\",\"version\":\"0.0.1\"}}}\n", idstr
                    fflush()
                }
                else if (line ~ /"method"[ ]*:[ ]*"tools\/list"/) {
                    printf "{\"jsonrpc\":\"2.0\",\"id\":%s,\"result\":{\"tools\":[", idstr
                    printf "{\"name\":\"echo\",\"description\":\"echoes input\",\"inputSchema\":{\"type\":\"object\"}},"
                    printf "{\"name\":\"upper\",\"description\":\"\",\"inputSchema\":{\"type\":\"object\"}},"
                    printf "{\"name\":\"reverse\",\"description\":\"\",\"inputSchema\":{\"type\":\"object\"}}"
                    printf "]}}\n"
                    fflush()
                }
                else if (line ~ /"method"[ ]*:[ ]*"tools\/call"/) {
                    printf "{\"jsonrpc\":\"2.0\",\"id\":%s,\"result\":{\"content\":[{\"type\":\"text\",\"text\":\"id=%s\"}],\"isError\":false}}\n", idstr, idtxt
                    fflush()
                }
            }'"#;
        ("sh", vec!["-c".into(), script.into()])
    }

    #[tokio::test]
    async fn handshake_happy_path() {
        if which::which("awk").is_err() || which::which("sh").is_err() {
            eprintln!("awk/sh not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_initialize_responder();
        let m = manifest(
            "fs-test",
            cmd,
            &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
            5_000,
        );
        let adapter = McpAdapter::new();
        adapter
            .register(m.clone(), tmp.path().to_path_buf())
            .await
            .unwrap();
        assert_eq!(
            adapter.status("fs-test").await.unwrap(),
            AdapterStatus::Idle
        );

        adapter
            .start_one("fs-test")
            .await
            .expect("handshake must succeed");

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
            .register(
                Arc::new(m),
                tempfile::tempdir().unwrap().path().to_path_buf(),
            )
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
        let m = manifest(
            "idem",
            cmd,
            &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
            5_000,
        );
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
        let m1 = manifest("zzz", "/definitely/not/a/real/binary/c2-iter4", &[], 5_000);
        let m2 = manifest("aaa", "/definitely/not/a/real/binary/c2-iter4", &[], 5_000);
        adapter
            .register(m1, tmp.path().to_path_buf())
            .await
            .unwrap();
        adapter
            .register(m2, tmp.path().to_path_buf())
            .await
            .unwrap();

        let _ = adapter.start_one("aaa").await;
        let _ = adapter.start_one("zzz").await;

        let s = adapter.statuses().await;
        assert_eq!(s.len(), 2);
        assert_eq!(s[0].0, "aaa");
        assert_eq!(s[1].0, "zzz");
        assert!(matches!(s[0].1, AdapterStatus::Failed(_)));
        assert!(matches!(s[1].1, AdapterStatus::Failed(_)));
    }

    // ----- Iter 5: tools/list + tools/call multiplex tests -----

    /// `tools/list` runs as part of `start_one`; an `All`-mode
    /// allowlist exports every upstream tool unchanged.
    #[tokio::test]
    async fn tools_list_full_export_under_all_mode() {
        if which::which("awk").is_err() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_initialize_responder();
        let m = manifest(
            "fs-all",
            cmd,
            &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
            5_000,
        );
        let adapter = McpAdapter::new();
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter
            .start_one("fs-all")
            .await
            .expect("start must succeed");

        let tools = adapter.tools_for("fs-all").await.unwrap();
        let names: Vec<&str> = tools.iter().map(|t| t.name.as_str()).collect();
        assert_eq!(names, vec!["echo", "upper", "reverse"]);
    }

    /// `tools_allowlist_filtered_by_allowlist` from the design test
    /// matrix: mode=allow, names=[echo,upper] keeps two tools, drops
    /// the third.
    #[tokio::test]
    async fn tools_list_filtered_by_allowlist_allow() {
        if which::which("awk").is_err() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_initialize_responder();
        let m = manifest_with(
            "fs-allow",
            cmd,
            &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
            5_000,
            Some(ToolsAllowlist {
                mode: AllowlistMode::Allow,
                names: vec!["echo".into(), "upper".into()],
            }),
        );
        let adapter = McpAdapter::new();
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter.start_one("fs-allow").await.unwrap();

        let names: Vec<String> = adapter
            .tools_for("fs-allow")
            .await
            .unwrap()
            .into_iter()
            .map(|t| t.name)
            .collect();
        assert_eq!(names, vec!["echo".to_string(), "upper".to_string()]);
    }

    /// Default mode (`Allow` + empty names) is fail-closed: zero tools
    /// exported even though the upstream offered three.
    #[tokio::test]
    async fn tools_list_default_allowlist_is_fail_closed() {
        if which::which("awk").is_err() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_initialize_responder();
        let m = manifest_with(
            "fs-closed",
            cmd,
            &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
            5_000,
            Some(ToolsAllowlist {
                mode: AllowlistMode::Allow,
                names: vec![],
            }),
        );
        let adapter = McpAdapter::new();
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter.start_one("fs-closed").await.unwrap();
        assert!(adapter.tools_for("fs-closed").await.unwrap().is_empty());
    }

    /// `Deny` mode: keep everything except the listed names.
    #[tokio::test]
    async fn tools_list_filtered_by_allowlist_deny() {
        if which::which("awk").is_err() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_initialize_responder();
        let m = manifest_with(
            "fs-deny",
            cmd,
            &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
            5_000,
            Some(ToolsAllowlist {
                mode: AllowlistMode::Deny,
                names: vec!["reverse".into()],
            }),
        );
        let adapter = McpAdapter::new();
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter.start_one("fs-deny").await.unwrap();

        let names: Vec<String> = adapter
            .tools_for("fs-deny")
            .await
            .unwrap()
            .into_iter()
            .map(|t| t.name)
            .collect();
        assert_eq!(names, vec!["echo".to_string(), "upper".to_string()]);
    }

    /// Calling a tool that wasn't in the allowlist must surface
    /// `UnknownTool` without sending a frame to the child (so a
    /// disabled tool can't leak through).
    #[tokio::test]
    async fn call_tool_rejects_unknown_tool() {
        if which::which("awk").is_err() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_initialize_responder();
        let m = manifest_with(
            "fs-unk",
            cmd,
            &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
            5_000,
            Some(ToolsAllowlist {
                mode: AllowlistMode::Allow,
                names: vec!["echo".into()],
            }),
        );
        let adapter = McpAdapter::new();
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter.start_one("fs-unk").await.unwrap();

        let err = adapter
            .call_tool(
                "fs-unk",
                "reverse",
                JsonValue::Object(Default::default()),
                Some(Duration::from_secs(2)),
            )
            .await
            .expect_err("unknown tool must reject");
        match err {
            AdapterError::UnknownTool { plugin, tool } => {
                assert_eq!(plugin, "fs-unk");
                assert_eq!(tool, "reverse");
            }
            other => panic!("expected UnknownTool, got {other:?}"),
        }
    }

    /// `tools/call` happy path returns the projected `CallResult`
    /// with `is_error = false`.
    #[tokio::test]
    async fn call_tool_happy_path() {
        if which::which("awk").is_err() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_initialize_responder();
        let m = manifest(
            "fs-call",
            cmd,
            &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
            5_000,
        );
        let adapter = McpAdapter::new();
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter.start_one("fs-call").await.unwrap();

        let res = adapter
            .call_tool(
                "fs-call",
                "echo",
                serde_json::json!({"x": 1}),
                Some(Duration::from_secs(2)),
            )
            .await
            .expect("call must succeed");
        assert!(!res.is_error);
        assert_eq!(res.content.len(), 1);
        match &res.content[0] {
            crate::runtime::mcp::schema::tools::Content::Text { text } => {
                assert!(text.starts_with("id="), "unexpected echo: {text:?}");
            }
            other => panic!("expected text content, got {other:?}"),
        }
    }

    /// Concurrency: 8 in-flight `tools/call`s share the same client
    /// and receive their own correctly-correlated responses (no
    /// cross-talk between ids). The awk responder echoes the request
    /// id verbatim into the result text, so a mismatched id would
    /// surface as a wrong text payload.
    #[tokio::test]
    async fn concurrent_tool_calls_multiplex_correctly() {
        if which::which("awk").is_err() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_initialize_responder();
        let m = manifest(
            "fs-mux",
            cmd,
            &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
            5_000,
        );
        let adapter = Arc::new(McpAdapter::new());
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter.start_one("fs-mux").await.unwrap();

        // Fire 8 calls concurrently; collect their text payloads.
        let mut handles = Vec::new();
        for _ in 0..8 {
            let a = Arc::clone(&adapter);
            handles.push(tokio::spawn(async move {
                a.call_tool(
                    "fs-mux",
                    "echo",
                    JsonValue::Object(Default::default()),
                    Some(Duration::from_secs(2)),
                )
                .await
            }));
        }
        let mut ids = Vec::new();
        for h in handles {
            let r = h.await.expect("join").expect("call must succeed");
            assert!(!r.is_error);
            assert_eq!(r.content.len(), 1);
            match &r.content[0] {
                crate::runtime::mcp::schema::tools::Content::Text { text } => {
                    let id = text.strip_prefix("id=").expect("id= prefix").to_string();
                    ids.push(id);
                }
                other => panic!("expected text content, got {other:?}"),
            }
        }
        // Every reply text must carry a *distinct* request id —
        // proves the demux didn't merge, drop, or duplicate frames.
        let mut sorted = ids.clone();
        sorted.sort();
        sorted.dedup();
        assert_eq!(sorted.len(), 8, "ids collided / duplicated: {ids:?}");
    }

    /// `filter_and_project_tools` unit-tests independent of any spawn:
    /// covers the three modes plus empty-names edge cases.
    #[test]
    fn filter_and_project_tools_three_modes() {
        let upstream = vec![
            mcp_tools::ToolDescriptor {
                name: "a".into(),
                description: Some("alpha".into()),
                input_schema: serde_json::json!({"type": "object"}),
            },
            mcp_tools::ToolDescriptor {
                name: "b".into(),
                description: None,
                input_schema: serde_json::json!({"type": "object"}),
            },
            mcp_tools::ToolDescriptor {
                name: "c".into(),
                description: Some("gamma".into()),
                input_schema: serde_json::json!({"type": "object"}),
            },
        ];

        let all = filter_and_project_tools(
            &upstream,
            &ToolsAllowlist {
                mode: AllowlistMode::All,
                names: vec![],
            },
        );
        assert_eq!(
            all.iter().map(|t| t.name.clone()).collect::<Vec<_>>(),
            vec!["a", "b", "c"]
        );

        let allow = filter_and_project_tools(
            &upstream,
            &ToolsAllowlist {
                mode: AllowlistMode::Allow,
                names: vec!["a".into(), "c".into()],
            },
        );
        assert_eq!(
            allow.iter().map(|t| t.name.clone()).collect::<Vec<_>>(),
            vec!["a", "c"]
        );

        let deny = filter_and_project_tools(
            &upstream,
            &ToolsAllowlist {
                mode: AllowlistMode::Deny,
                names: vec!["b".into()],
            },
        );
        assert_eq!(
            deny.iter().map(|t| t.name.clone()).collect::<Vec<_>>(),
            vec!["a", "c"]
        );

        // Description fallback: None -> "" in projected Tool.
        assert_eq!(all[1].description, "");
        assert_eq!(all[0].description, "alpha");
    }

    // ----- Iter 8: disable / enable / restart admin surface -----

    /// `disable_one` stops the child, writes the `.disabled` sentinel,
    /// and flips `is_disabled` to true. A subsequent `start_one` is a
    /// no-op that surfaces `Disabled` so admin sees the intended state.
    #[tokio::test]
    async fn disable_one_persists_sentinel_and_blocks_start() {
        if which::which("awk").is_err() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_initialize_responder();
        let m = manifest(
            "dis",
            cmd,
            &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
            5_000,
        );
        let adapter = McpAdapter::new();
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter.start_one("dis").await.unwrap();
        assert!(adapter.is_alive("dis").await.unwrap());

        adapter.disable_one("dis").await.unwrap();
        assert!(adapter.is_disabled("dis").await.unwrap());
        assert!(
            !adapter.is_alive("dis").await.unwrap(),
            "child must be down"
        );
        assert_eq!(adapter.status("dis").await.unwrap(), AdapterStatus::Stopped);

        // Sentinel exists on disk.
        let sentinel = tmp.path().join(DISABLED_SENTINEL_FILENAME);
        assert!(sentinel.exists(), "sentinel must persist on disk");

        // start_one returns Disabled.
        let err = adapter
            .start_one("dis")
            .await
            .expect_err("disabled slot must reject start_one");
        assert!(
            matches!(err, AdapterError::Disabled(_)),
            "expected Disabled, got {err:?}"
        );
    }

    /// `enable_one` removes the sentinel and clears the flag. A
    /// subsequent `start_one` succeeds.
    #[tokio::test]
    async fn enable_one_clears_sentinel_and_allows_start() {
        if which::which("awk").is_err() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_initialize_responder();
        let m = manifest(
            "ena",
            cmd,
            &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
            5_000,
        );
        let adapter = McpAdapter::new();
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter.start_one("ena").await.unwrap();
        adapter.disable_one("ena").await.unwrap();

        adapter.enable_one("ena").await.unwrap();
        assert!(!adapter.is_disabled("ena").await.unwrap());
        let sentinel = tmp.path().join(DISABLED_SENTINEL_FILENAME);
        assert!(!sentinel.exists(), "sentinel must be removed");

        adapter
            .start_one("ena")
            .await
            .expect("start must succeed after enable");
        assert!(adapter.is_alive("ena").await.unwrap());
    }

    /// A `.disabled` sentinel left over from a previous gateway run
    /// keeps the slot disabled across re-register: the new adapter
    /// honours the file without admin reasserting the disable.
    #[tokio::test]
    async fn disabled_sentinel_persists_across_register() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join(DISABLED_SENTINEL_FILENAME), b"disabled\n").unwrap();

        let m = manifest(
            "ghost-disabled",
            "/definitely/not/real/c2-iter8",
            &[],
            5_000,
        );
        let adapter = McpAdapter::new();
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        assert!(adapter.is_disabled("ghost-disabled").await.unwrap());

        // start_one rejects without trying to spawn (the binary doesn't
        // exist, so a *Spawn* error here would mean the disabled gate
        // missed).
        let err = adapter
            .start_one("ghost-disabled")
            .await
            .expect_err("disabled-on-register must reject start");
        assert!(
            matches!(err, AdapterError::Disabled(_)),
            "expected Disabled, got {err:?}"
        );
    }

    /// `disable_one` is idempotent: a second call on an already-
    /// disabled slot succeeds without rewriting the sentinel.
    #[tokio::test]
    async fn disable_one_is_idempotent() {
        let tmp = tempfile::tempdir().unwrap();
        let m = manifest("idem-dis", "/no-binary", &[], 5_000);
        let adapter = McpAdapter::new();
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter.disable_one("idem-dis").await.unwrap();
        adapter
            .disable_one("idem-dis")
            .await
            .expect("idempotent second disable must succeed");
        assert!(adapter.is_disabled("idem-dis").await.unwrap());
    }

    /// `enable_one` is idempotent: a second call (or a call on a
    /// never-disabled slot) is a no-op.
    #[tokio::test]
    async fn enable_one_is_idempotent_when_not_disabled() {
        let tmp = tempfile::tempdir().unwrap();
        let m = manifest("idem-ena", "/no-binary", &[], 5_000);
        let adapter = McpAdapter::new();
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter
            .enable_one("idem-ena")
            .await
            .expect("enable on already-enabled slot is no-op");
        assert!(!adapter.is_disabled("idem-ena").await.unwrap());
    }

    /// `restart_one` brings a `Stopped` slot back to `Initialized`
    /// without admin needing two calls.
    #[tokio::test]
    async fn restart_one_recovers_stopped_slot() {
        if which::which("awk").is_err() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_initialize_responder();
        let m = manifest(
            "rest",
            cmd,
            &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
            5_000,
        );
        let adapter = McpAdapter::new();
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter.start_one("rest").await.unwrap();
        adapter.stop_one("rest").await.unwrap();
        assert_eq!(
            adapter.status("rest").await.unwrap(),
            AdapterStatus::Stopped
        );

        adapter.restart_one("rest").await.expect("restart succeeds");
        assert_eq!(
            adapter.status("rest").await.unwrap(),
            AdapterStatus::Initialized
        );
    }

    /// `restart_one` on a disabled slot returns `Disabled` without
    /// touching the child (sentinel takes precedence over restart
    /// requests).
    #[tokio::test]
    async fn restart_one_respects_disabled_state() {
        let tmp = tempfile::tempdir().unwrap();
        let m = manifest("rest-dis", "/no-binary", &[], 5_000);
        let adapter = McpAdapter::new();
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter.disable_one("rest-dis").await.unwrap();
        let err = adapter
            .restart_one("rest-dis")
            .await
            .expect_err("disabled slot must reject restart");
        assert!(
            matches!(err, AdapterError::Disabled(_)),
            "expected Disabled, got {err:?}"
        );
    }

    /// `call_tool` on a disabled slot rejects with `Disabled` even if
    /// the slot was previously initialized (the disable transition
    /// stops the child, so the resolved tools list also clears).
    #[tokio::test]
    async fn call_tool_rejects_when_disabled() {
        if which::which("awk").is_err() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_initialize_responder();
        let m = manifest(
            "dis-call",
            cmd,
            &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
            5_000,
        );
        let adapter = McpAdapter::new();
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter.start_one("dis-call").await.unwrap();
        adapter.disable_one("dis-call").await.unwrap();

        let err = adapter
            .call_tool(
                "dis-call",
                "echo",
                JsonValue::Object(Default::default()),
                Some(Duration::from_secs(1)),
            )
            .await
            .expect_err("disabled slot must reject calls");
        assert!(
            matches!(err, AdapterError::Disabled(_)),
            "expected Disabled, got {err:?}"
        );
    }
}
