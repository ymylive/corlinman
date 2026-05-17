# corlinman-mcp-server

Python port of the Rust `corlinman-mcp` crate. Implements an MCP
(Model Context Protocol, 2024-11-05) server over WebSocket plus a
matching stdio client peer.

The package is wire-compatible with the Rust server: the same Claude
Desktop fixture round-trips through both, byte-for-byte (modulo the
top-level `id` and `serverInfo.version` fields).

Public surface mirrors the crate 1:1:

* `McpServer` / `McpServerConfig` — WebSocket server, `/mcp` route
* `AdapterDispatcher` / `ServerInfo` — JSON-RPC dispatcher
* `ToolsAdapter` / `ResourcesAdapter` / `PromptsAdapter` — capability bridges
* `SessionContext`, `SessionState`, `SessionPhase`
* `TokenAcl`, `resolve_token`, `DEFAULT_TENANT_ID`
* `McpError`, `JsonRpcError`, `JsonRpcRequest`, `JsonRpcResponse`
* `MCP_PROTOCOL_VERSION` (`"2024-11-05"`), `JSONRPC_VERSION` (`"2.0"`)
* `error_codes` — JSON-RPC 2.0 §5.1 numeric codes + corlinman extensions
* `McpClient` / `McpClientError` — outbound stdio JSON-RPC peer
