"""``corlinman_server.gateway.mcp`` — gateway integration of the MCP
``/mcp`` server.

Port of :rust:`corlinman_gateway::mcp`. Reads the
:class:`McpConfig`-equivalent (a thin dataclass mirroring
``corlinman::config::McpConfig``), builds a
:class:`corlinman_mcp_server.McpServer` with an adapter-backed
dispatcher, and returns it ready for the gateway boot path to bind.

Design pin (matches the Rust comment): the MCP block is in
``RESTART_REQUIRED_SECTIONS`` — a hot-reload of ``[mcp]`` lands in
config storage but the live listener does not pick the change up.
Operators are expected to restart the gateway when the MCP block
changes (a ``mcp.restart_required`` event is wired into the gateway
config watcher elsewhere).

Build path (mirrors the Rust steps 1-4):

1. Translate :class:`McpTokenConfig` items → :class:`TokenAcl`. An
   empty / blank ``tenant_id`` falls back to
   :data:`corlinman_mcp_server.DEFAULT_TENANT_ID`.
2. Build the three adapters (tools / resources / prompts) from the
   live :class:`PluginRegistry`, :class:`SkillRegistry`, and a
   ``{tenant_slug: MemoryHost}`` map. The persona adapter starts as
   :class:`NullPersonaProvider` (mirrors the Rust C1 default).
3. Construct an :class:`AdapterDispatcher` and hand it to
   :class:`McpServer`.
4. Return the server so the boot path can ``await server.bind(...)``
   or compose the underlying handler into a larger transport stack.
"""

from __future__ import annotations

from corlinman_server.gateway.mcp.server import (
    McpConfig,
    McpServerSection,
    McpTokenConfig,
    build_dispatcher,
    build_mcp_server,
    build_server_config,
    token_config_to_acl,
)

__all__ = [
    "McpConfig",
    "McpServerSection",
    "McpTokenConfig",
    "build_dispatcher",
    "build_mcp_server",
    "build_server_config",
    "token_config_to_acl",
]
