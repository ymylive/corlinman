"""Gateway-side MCP server bootstrap.

Port of :rust:`corlinman_gateway::mcp`. Reads the ``[mcp]`` config
section (modelled as the dataclasses below to keep this module free of
a Rust-config dep), translates it to a
:class:`corlinman_mcp_server.McpServerConfig`, builds the dispatcher
+ three capability adapters (tools / resources / prompts), and hands
back a ready-to-bind :class:`corlinman_mcp_server.McpServer`.

Why a thin wrapper?
-------------------
The Rust crate returns an ``axum::Router`` so the boot path can
``Router::merge`` it onto the rest of the gateway's HTTP surface.
The Python ``corlinman_mcp_server`` package exposes a standalone
WebSocket server — there is no `axum`-shaped router to merge — so
this module's :func:`build_mcp_server` returns the
:class:`McpServer` instance and the gateway boot path calls
``await server.bind(host, port)`` to start serving.

TODO (matches the Rust comment, plus Python-specific gaps):
* Wire the live ``PluginRegistry`` / ``SkillRegistry`` from the
  gateway state once those Python sibling packages land. The
  current build-path signature accepts them as **arguments** so the
  boot path stays the single source of truth.
* Tenant-routed ``memory_hosts``: the Rust C1 path passes the same
  ``BTreeMap`` for every token; cross-tenant scoping is enforced by
  the ``SessionContext.tenant_id`` the adapter consults. We
  preserve the same shape — a ``{tenant_slug: MemoryHost}`` dict
  with the same semantics — so a later tenant-routing layer drops
  in without changing this surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import metadata as _pkg_metadata

from corlinman_mcp_server import (
    AdapterDispatcher,
    DEFAULT_TENANT_ID,
    McpServer,
    McpServerConfig,
    MemoryHost,
    PluginRegistry,
    PluginRuntime,
    PromptsAdapter,
    ResourcesAdapter,
    ServerInfo,
    SkillRegistry,
    TokenAcl,
    ToolsAdapter,
)

__all__ = [
    "DEFAULT_MAX_FRAME_BYTES",
    "McpConfig",
    "McpServerSection",
    "McpTokenConfig",
    "build_dispatcher",
    "build_mcp_server",
    "build_server_config",
    "token_config_to_acl",
]


# Default cap on inbound frames (1 MiB) — matches the value the Python
# ``corlinman_mcp_server`` package defaults to. Surfaced here so a
# pyproject-shaped ``McpConfig`` builder can default it without
# importing the lower-level module.
DEFAULT_MAX_FRAME_BYTES: int = 1_048_576


# ─── Config dataclasses (mirror corlinman::config::McpConfig) ────────


@dataclass
class McpTokenConfig:
    """One ACL entry in the gateway's ``[mcp.server.tokens]`` array.

    Mirrors :rust:`corlinman_core::config::McpTokenConfig`. Field
    names + types track the Rust struct so a future config-port can
    hydrate this directly from TOML.
    """

    token: str
    label: str = "permissive"
    tools_allowlist: list[str] = field(default_factory=list)
    resources_allowed: list[str] = field(default_factory=list)
    prompts_allowed: list[str] = field(default_factory=list)
    tenant_id: str | None = None


@dataclass
class McpServerSection:
    """``[mcp.server]`` subsection. Mirrors
    :rust:`corlinman_core::config::McpServerSection`."""

    bind: str = "127.0.0.1:0"
    allowed_origins: list[str] = field(default_factory=list)
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES
    inactivity_timeout_secs: int = 300
    heartbeat_secs: int = 20
    max_concurrent_sessions: int = 4
    tokens: list[McpTokenConfig] = field(default_factory=list)


@dataclass
class McpConfig:
    """``[mcp]`` section. Mirrors
    :rust:`corlinman_core::config::McpConfig`."""

    enabled: bool = False
    server: McpServerSection = field(default_factory=McpServerSection)


# ─── Translators ──────────────────────────────────────────────────────


def token_config_to_acl(t: McpTokenConfig) -> TokenAcl:
    """Translate one config-shape :class:`McpTokenConfig` into a runtime
    :class:`TokenAcl`. Empty / missing ``tenant_id`` falls back to
    :data:`DEFAULT_TENANT_ID` via :meth:`TokenAcl.effective_tenant`.
    Mirrors :rust:`token_config_to_acl`.
    """
    return TokenAcl(
        token=t.token,
        label=t.label,
        tools_allowlist=list(t.tools_allowlist),
        resources_allowed=list(t.resources_allowed),
        prompts_allowed=list(t.prompts_allowed),
        tenant_id=t.tenant_id,
    )


def build_server_config(cfg: McpConfig) -> McpServerConfig:
    """Build an :class:`McpServerConfig` from the gateway's
    :class:`McpConfig`. Mirrors :rust:`build_server_config`."""
    return McpServerConfig(
        tokens=[token_config_to_acl(t) for t in cfg.server.tokens],
        max_frame_bytes=int(cfg.server.max_frame_bytes),
    )


def build_dispatcher(
    plugins: PluginRegistry,
    skills: SkillRegistry,
    memory_hosts: dict[str, MemoryHost],
    runtime: PluginRuntime,
) -> AdapterDispatcher:
    """Build the dispatcher from the live registries + memory hosts.

    Memory hosts arrive as a tenant-keyed dict so a future tenant-
    routing layer can pre-prune the visible set per token. The C1 path
    passes the same map for every token; cross-tenant scoping is
    enforced by the ``tenant_id`` on the :class:`SessionContext` that
    the adapter consults. Mirrors :rust:`build_dispatcher`.
    """
    tools_adapter = ToolsAdapter.with_runtime(plugins, runtime)
    resources_adapter = ResourcesAdapter(memory_hosts, skills)
    prompts_adapter = PromptsAdapter(skills)
    return AdapterDispatcher.from_adapters(
        ServerInfo(
            name="corlinman",
            version=_pkg_version(),
        ),
        [tools_adapter, resources_adapter, prompts_adapter],
    )


def build_mcp_server(
    cfg: McpConfig,
    plugins: PluginRegistry,
    skills: SkillRegistry,
    memory_hosts: dict[str, MemoryHost],
    runtime: PluginRuntime,
) -> McpServer | None:
    """Build the gateway's :class:`McpServer`.

    Returns ``None`` when ``[mcp].enabled = False`` so the boot path
    can omit the bind entirely (mirrors the Rust ``build_router``
    return-``Option`` contract).

    The caller is responsible for binding the server, e.g.::

        server = build_mcp_server(cfg, plugins, skills, hosts, runtime)
        if server is not None:
            ws_server = await server.bind(host, port)
            # ... keep ws_server, ``await ws_server.wait_closed()`` on shutdown.
    """
    if not cfg.enabled:
        return None
    server_cfg = build_server_config(cfg)
    dispatcher = build_dispatcher(plugins, skills, memory_hosts, runtime)
    return McpServer(server_cfg, dispatcher)


# ─── Helpers ──────────────────────────────────────────────────────────


def _pkg_version() -> str:
    """Resolve the corlinman-server package version. Mirrors the Rust
    ``env!("CARGO_PKG_VERSION")`` lookup — we hit installed package
    metadata first and fall back to a hardcoded sentinel so this is
    safe to call from a fresh repo without an editable install.
    """
    try:
        return _pkg_metadata.version("corlinman-server")
    except _pkg_metadata.PackageNotFoundError:
        return "0.0.0+local"
