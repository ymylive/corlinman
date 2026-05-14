//! Phase 4 W3 C1 iter 9 — gateway integration of the MCP `/mcp` server.
//!
//! Reads `[mcp]` from `corlinman::config::Config`, builds a
//! [`corlinman_mcp::McpServer`] with adapter-backed dispatcher, and
//! returns the axum [`Router`] for `mount_mcp_router` to merge alongside
//! the rest of the gateway surface.
//!
//! Design pin: the MCP block is in [`RESTART_REQUIRED_SECTIONS`]
//! (see `config_watcher.rs:56`) — a hot-reload of `[mcp]` lands in the
//! `ArcSwap<Config>` but the live `/mcp` listener does not pick the
//! change up. Operators are warned via the `mcp.restart_required`
//! event. (Intentional: re-binding a WebSocket listener mid-run is
//! racy in a way the C1 scope doesn't justify.)
//!
//! ## Build path
//!
//! 1. Translate `Vec<McpTokenConfig>` → `Vec<TokenAcl>`. Empty / blank
//!    `tenant_id` falls back to `DEFAULT_TENANT_ID`.
//! 2. Build the three adapters from the live `PluginRegistry`,
//!    `SkillRegistry`, and a `BTreeMap<String, Arc<dyn MemoryHost>>`
//!    (empty by default in C1; future iters wire `TenantPool::pool_for`
//!    here). The persona adapter starts as `NullPersonaProvider` —
//!    that surface is Python-side today.
//! 3. Construct `AdapterDispatcher` and hand it to
//!    `McpServer::new(McpServerConfig{...}, dispatcher)`.
//! 4. Return the server's router so the boot path can `Router::merge`
//!    it onto the base.

use std::collections::BTreeMap;
use std::sync::Arc;

use axum::Router;

use corlinman_core::config::McpConfig;
use corlinman_mcp::adapters::{ResourcesAdapter, ToolsAdapter};
use corlinman_mcp::server::{
    AdapterDispatcher, FrameHandler, McpServer, McpServerConfig, ServerInfo, TokenAcl,
};
use corlinman_mcp::CapabilityAdapter;
use corlinman_memory_host::MemoryHost;
use corlinman_plugins::registry::PluginRegistry;
use corlinman_plugins::runtime::{jsonrpc_stdio::JsonRpcStdioRuntime, PluginRuntime};
use corlinman_skills::SkillRegistry;

/// Translate one config-shape `McpTokenConfig` into a runtime
/// `TokenAcl`. Empty `tenant_id` falls back to the default tenant.
pub fn token_config_to_acl(t: &corlinman_core::config::McpTokenConfig) -> TokenAcl {
    TokenAcl {
        token: t.token.clone(),
        label: t.label.clone(),
        tools_allowlist: t.tools_allowlist.clone(),
        resources_allowed: t.resources_allowed.clone(),
        prompts_allowed: t.prompts_allowed.clone(),
        tenant_id: t.tenant_id.clone(),
    }
}

/// Build an [`McpServerConfig`] from the gateway's [`McpConfig`].
pub fn build_server_config(cfg: &McpConfig) -> McpServerConfig {
    McpServerConfig {
        tokens: cfg.server.tokens.iter().map(token_config_to_acl).collect(),
        max_frame_bytes: cfg.server.max_frame_bytes as usize,
    }
}

/// Build the dispatcher from the live registries + memory hosts.
/// Memory hosts arrive as a tenant-keyed `BTreeMap<String, ...>` so a
/// future tenant-routing layer can pre-prune the visible set per token.
/// The C1 path passes the same map for every token; cross-tenant
/// scoping is enforced by the `tenant_id` on the
/// `SessionContext` that the adapter consults.
pub fn build_dispatcher(
    plugins: Arc<PluginRegistry>,
    skills: Arc<SkillRegistry>,
    memory_hosts: BTreeMap<String, Arc<dyn MemoryHost>>,
    runtime: Arc<dyn PluginRuntime>,
) -> AdapterDispatcher {
    let tools =
        Arc::new(ToolsAdapter::with_runtime(plugins, runtime)) as Arc<dyn CapabilityAdapter>;
    let resources =
        Arc::new(ResourcesAdapter::new(memory_hosts, skills.clone())) as Arc<dyn CapabilityAdapter>;
    let prompts = Arc::new(corlinman_mcp::adapters::PromptsAdapter::new(skills))
        as Arc<dyn CapabilityAdapter>;
    AdapterDispatcher::from_adapters(
        ServerInfo {
            name: "corlinman".into(),
            version: env!("CARGO_PKG_VERSION").into(),
        },
        vec![tools, resources, prompts],
    )
}

/// Build the `/mcp` router. Returns `None` when `[mcp].enabled =
/// false`, so the boot path can omit the merge entirely. Returns
/// `Some(router)` otherwise — the caller `Router::merge`s it.
pub fn build_router(
    cfg: &McpConfig,
    plugins: Arc<PluginRegistry>,
    skills: Arc<SkillRegistry>,
    memory_hosts: BTreeMap<String, Arc<dyn MemoryHost>>,
) -> Option<Router> {
    if !cfg.enabled {
        return None;
    }
    let runtime: Arc<dyn PluginRuntime> = Arc::new(JsonRpcStdioRuntime);
    Some(build_router_with_runtime(
        cfg,
        plugins,
        skills,
        memory_hosts,
        runtime,
    ))
}

/// Variant that accepts a custom [`PluginRuntime`]. Used by the
/// integration tests so we can inject a stub runtime instead of
/// shelling out to a real plugin process.
pub fn build_router_with_runtime(
    cfg: &McpConfig,
    plugins: Arc<PluginRegistry>,
    skills: Arc<SkillRegistry>,
    memory_hosts: BTreeMap<String, Arc<dyn MemoryHost>>,
    runtime: Arc<dyn PluginRuntime>,
) -> Router {
    let server_cfg = build_server_config(cfg);
    let dispatcher = build_dispatcher(plugins, skills, memory_hosts, runtime);
    let dispatcher: Arc<dyn FrameHandler> = Arc::new(dispatcher);
    let server = McpServer::new(server_cfg, dispatcher);
    server.router()
}

#[cfg(test)]
mod tests {
    use super::*;
    use corlinman_core::config::{McpServerSection, McpTokenConfig};

    #[test]
    fn token_config_to_acl_round_trips_fields() {
        let t = McpTokenConfig {
            token: "abc".into(),
            label: "lap".into(),
            tools_allowlist: vec!["kb:*".into()],
            resources_allowed: vec!["skill".into()],
            prompts_allowed: vec!["*".into()],
            tenant_id: Some("alpha".into()),
        };
        let acl = token_config_to_acl(&t);
        assert_eq!(acl.token, "abc");
        assert_eq!(acl.label, "lap");
        assert_eq!(acl.tenant_id.as_deref(), Some("alpha"));
        assert_eq!(acl.effective_tenant(), "alpha");
    }

    #[test]
    fn token_config_without_tenant_falls_back_to_default() {
        let t = McpTokenConfig {
            token: "x".into(),
            label: "no-tenant".into(),
            tools_allowlist: vec![],
            resources_allowed: vec![],
            prompts_allowed: vec![],
            tenant_id: None,
        };
        let acl = token_config_to_acl(&t);
        assert_eq!(acl.effective_tenant(), "default");
    }

    #[test]
    fn build_router_returns_none_when_disabled() {
        let cfg = McpConfig {
            enabled: false,
            ..Default::default()
        };
        let plugins = Arc::new(PluginRegistry::default());
        let skills = Arc::new(SkillRegistry::default());
        let hosts: BTreeMap<String, Arc<dyn MemoryHost>> = BTreeMap::new();
        let r = build_router(&cfg, plugins, skills, hosts);
        assert!(r.is_none(), "disabled config must yield no router");
    }

    #[test]
    fn build_server_config_carries_frame_cap_and_tokens() {
        let cfg = McpConfig {
            server: McpServerSection {
                bind: "127.0.0.1:18791".into(),
                allowed_origins: vec![],
                max_frame_bytes: 8192,
                inactivity_timeout_secs: 300,
                heartbeat_secs: 20,
                max_concurrent_sessions: 4,
                tokens: vec![McpTokenConfig {
                    token: "tok-1".into(),
                    label: "lap".into(),
                    tools_allowlist: vec!["*".into()],
                    resources_allowed: vec!["*".into()],
                    prompts_allowed: vec!["*".into()],
                    tenant_id: None,
                }],
            },
            ..Default::default()
        };
        let s = build_server_config(&cfg);
        assert_eq!(s.max_frame_bytes, 8192);
        assert_eq!(s.tokens.len(), 1);
        assert_eq!(s.tokens[0].token, "tok-1");
    }
}
