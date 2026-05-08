# Phase 4 W3 C1 — MCP server (`/mcp` WebSocket)

**Status**: Design (pre-implementation) · **Owner**: TBD · **Created**: 2026-05-08 · **Estimate**: 5-7d

> A new `corlinman-mcp` crate hosts a Model Context Protocol server
> mounted at `/mcp` on the gateway. Claude Desktop (and any MCP
> 2024-11-05 client) connects, lists corlinman tools / resources /
> prompts, and invokes them. Server-only here; the plugin-adapter
> direction (`kind = "mcp"` corlinman tool) is C2.

Design seed for the iterations that follow. Pins the JSON-RPC schema,
the WebSocket transport, the auth gate, the three capability adapters
(tools ↔ `corlinman-plugins`, resources ↔ memory + persona, prompts
↔ `corlinman-skills`), and the test matrix against Claude Desktop's
MCP client. Mirrors `phase4-w2-b1-design.md` in shape.

## Why this exists

The only outside-facing reach into corlinman's tools / skills /
memory today is the OpenAI-shaped `/v1/chat/completions` route
(`routes/chat.rs`) — i.e. callers must already be using the gateway
as their model. Claude Desktop and any MCP client speak JSON-RPC 2.0
framed over stdio or HTTP+SSE/WebSocket, with a fixed vocabulary
(`tools/list`, `tools/call`, `resources/list`, `resources/read`,
`prompts/list`, `prompts/get`, `initialize`, plus notifications).
Roadmap row 4-3A (`phase4-roadmap.md:280`) pins the acceptance
criterion: "tested against Claude Desktop's MCP client."

C1 lets the operator point Desktop at `ws://gateway/mcp?token=…` and
every manifest-declared tool, every loaded skill, and the configured
memory hosts surface as native Claude capabilities — no per-surface
adapter. C2 is the inverse: any MCP-stdio server becomes a corlinman
plugin via `kind = "mcp"`. C1 ships the wire + schema crate C2
reuses.

No prior MCP scaffolding exists (`grep -rn mcp` on `rust/` +
`python/` is empty at design time). The `[mcp.server]` config block
is already pinned in roadmap §5 (`phase4-roadmap.md:366-368`).

## What MCP exposes — the three capability kinds

MCP 2024-11-05 defines exactly three server-offered capability
families (plus `sampling`, which we don't expose: corlinman's
gateway is a *server*, not a sampling client). Mapping:

| MCP capability | Wire methods | corlinman concept | Source crate |
|---|---|---|---|
| `tools` | `tools/list`, `tools/call` | Plugin tool invocations | `corlinman-plugins::PluginRegistry` (`registry.rs:139`) → `PluginRuntime::execute` (`runtime/mod.rs:77`) |
| `resources` | `resources/list`, `resources/read`, `resources/subscribe`* | Memory hits + persona snapshots + skill bodies (read-only) | `corlinman-memory-host::MemoryHost` (`lib.rs:38`), `corlinman-persona`, `corlinman-skills::SkillRegistry` |
| `prompts` | `prompts/list`, `prompts/get` | Skill metadata as parameterised prompts | `corlinman-skills::Skill` (`skill.rs:23`) |

`resources/subscribe` is **not** shipped in C1 (see Out of scope);
static `list` + `read` covers Desktop's "drag a resource into chat"
UX. Skill → MCP prompt mapping: frontmatter description + name carry
over, body becomes a single `user` message, `arguments` is empty
(skills lack a parameter schema today; C2-or-later extends
`SkillRequirements`).

## Protocol surface — `/mcp` WebSocket only

The MCP spec allows three transports: stdio (used by Desktop's
local plugins), HTTP+SSE (legacy 2024-04 spec), and WebSocket
(2024-11-05). Decision: **WebSocket only in C1**.

| Option | Pros | Cons |
|---|---|---|
| stdio | What Desktop's bundled plugins use | corlinman runs as a service, not a child process; can't be launched by Desktop |
| HTTP+SSE | Works through any HTTP proxy | Two endpoints (POST for client→server, SSE for server→client); state mgmt is harder; deprecated in 2025-03 spec draft |
| WebSocket | Single connection, full-duplex, mirrors `corlinman-wstool` server pattern (`server.rs:25-273`) | Some corp proxies block ws:// |

Single endpoint:

```text
GET /mcp?token=<bearer>
  → 101 Switching Protocols (axum WebSocketUpgrade)
```

Auth in the query string mirrors `wstool/connect`
(`corlinman-wstool/src/server.rs:251-273`): plain HTTP 401 *before*
upgrade, distinguishable from successful upgrade + JSON-RPC error.
`Authorization: Bearer` is RFC-cleaner but Desktop's config UI is
one URL field, so query-string fits.

### Session lifecycle (per JSON-RPC 2024-11-05)

```text
1. Client connects:        WS upgrade
2. Client sends:           {"method":"initialize","params":{...}}
3. Server replies:         {"result":{"capabilities":{...},"serverInfo":{...}}}
4. Client sends:           {"method":"notifications/initialized"} (no id)
5. <client → server>:      {"method":"tools/list"} ... etc, fully duplex
6. Either side closes WS → session terminates; outstanding tool calls cancel
```

One connection = one MCP session. No multi-session muxing on a
single socket; if Desktop opens two windows it dials twice.

## Crate layout

```
rust/crates/corlinman-mcp/
├── Cargo.toml
├── src/
│   ├── lib.rs            # public API (re-exports), feature gates
│   ├── schema.rs         # JSON-RPC frames + capability payloads (serde)
│   ├── error.rs          # McpError + JsonRpcError mapping
│   ├── server/
│   │   ├── mod.rs        # McpServer (axum Router builder)
│   │   ├── transport.rs  # WebSocket upgrade + connection loop
│   │   ├── session.rs    # SessionState (initialize handshake state machine)
│   │   ├── dispatch.rs   # method → adapter dispatcher
│   │   └── auth.rs       # token validation, ACL
│   └── adapters/
│       ├── mod.rs        # CapabilityAdapter trait
│       ├── tools.rs      # PluginRegistry → tools/list, tools/call
│       ├── resources.rs  # MemoryHost + PersonaStore → resources/list, resources/read
│       └── prompts.rs    # SkillRegistry → prompts/list, prompts/get
└── tests/
    ├── handshake.rs        # initialize → initialized
    ├── tools_roundtrip.rs  # list + call against a stub PluginRegistry
    ├── resources.rs        # list + read against a stub MemoryHost
    ├── prompts.rs          # list + get against a stub SkillRegistry
    └── desktop_fixture.rs  # replays a captured Claude Desktop trace
```

C2's outbound client reuses `schema.rs` and `error.rs` verbatim —
same wire types, opposite direction. C2 adds `client::*`, not
mounted under `server`.

## Schema / wire types

JSON-RPC 2.0 envelope, MCP 2024-11-05 method vocabulary. All types
in `schema.rs`, no axum / sqlx imports — keeps the schema crate
reusable by C2.

```rust
// JSON-RPC 2.0 envelope. id is i64 | string | null per spec; we
// take a string for simplicity (Desktop sends string ids).
#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "jsonrpc", rename = "2.0")]
pub struct JsonRpcRequest {
    pub id: Option<JsonValue>,    // None = notification
    pub method: String,
    #[serde(default)]
    pub params: JsonValue,
}

#[derive(Debug, Serialize, Deserialize)]
pub enum JsonRpcResponse {
    Result { id: JsonValue, result: JsonValue },
    Error  { id: JsonValue, error: JsonRpcError },
}

#[derive(Debug, Serialize, Deserialize)]
pub struct JsonRpcError {
    pub code: i32,           // -32600 invalid req, -32601 method not found, etc.
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<JsonValue>,
}

// MCP capability payloads. One module per family.
pub mod tools {
    pub struct ListResult { pub tools: Vec<ToolDescriptor> }
    pub struct ToolDescriptor {
        pub name: String,
        pub description: String,
        pub input_schema: JsonValue,  // JSON Schema for arguments
    }
    pub struct CallParams {
        pub name: String,
        pub arguments: JsonValue,
    }
    pub struct CallResult {
        pub content: Vec<Content>,    // text | image | resource_link
        #[serde(default, rename = "isError")]
        pub is_error: bool,
    }
}
pub mod resources { /* Resource, ResourceContent, ListResult, ReadParams, ReadResult */ }
pub mod prompts   { /* Prompt, PromptArgument, ListResult, GetParams, GetResult */ }
```

Reference: MCP spec 2024-11-05. Future revs go behind
`protocolVersion` negotiation in `initialize`.

## Capability adapters

The `CapabilityAdapter` trait is the bridge. Each adapter wraps an
existing corlinman primitive and emits the MCP-shaped frames. Trait
keeps the dispatcher generic and lets tests stub each surface
independently.

```rust
#[async_trait]
pub trait CapabilityAdapter: Send + Sync {
    fn capability_name(&self) -> &'static str;       // "tools" | "resources" | "prompts"
    async fn handle(
        &self,
        method: &str,
        params: JsonValue,
        ctx: &SessionContext,
    ) -> Result<JsonValue, McpError>;
}
```

### `tools` adapter — `PluginRegistry` → MCP

- `tools/list`: iterate `PluginRegistry::list()`
  (`registry.rs:139`); per `PluginEntry`, expand
  `manifest.capabilities.tools` into MCP `ToolDescriptor`. Tool
  name is `<plugin>:<tool>` (Open question §2). `input_schema`
  comes from the manifest; absent → `{"type":"object",
  "additionalProperties":true}`.
- `tools/call`: split on `:` → `(plugin, tool)`; build
  `PluginInput`, dispatch via `protocol::dispatcher`
  (`protocol/dispatcher.rs:77`). Map `PluginOutput`:
  - `Content` → `CallResult` with one `text` block.
  - `Error { code, message }` → `CallResult { is_error: true,
    content: [{"type":"text","text":message}] }` (MCP convention:
    JSON-RPC error is for protocol-level failures only).

Cancellation: client closing WS aborts in-flight calls via the
session-scoped `CancellationToken`. `ProgressSink` calls bridge to
MCP `notifications/progress` frames (see Open questions §5).

### `resources` adapter — memory + persona + skill bodies

Three kinds of resources surface; all read-only.

| URI scheme | Source | Listing | Read |
|---|---|---|---|
| `corlinman://memory/<host>/<id>` | `MemoryHost::query` | One row per top-N hit, query=`""` enumeration | `MemoryHost::get(id)` (new method — see Open questions) |
| `corlinman://persona/<user_id>/snapshot` | `corlinman-persona` store | Per-user trait snapshot | Trait JSON serialised |
| `corlinman://skill/<name>` | `SkillRegistry::iter()` | All loaded skills | `Skill.body_markdown` |

`resources/list` paginates via the spec's `cursor`; default page
size 100. The composite adapter holds `Arc<dyn MemoryHost>` +
`Arc<PersonaStore>` + `Arc<SkillRegistry>` and round-robins listing.

### `prompts` adapter — skill metadata as MCP prompts

- `prompts/list`: `SkillRegistry::iter()` → one `Prompt` per skill
  with empty `arguments` array.
- `prompts/get { name }`: load skill by name; return one `user`
  message whose content is `skill.body_markdown`. `description` =
  `skill.description`. Unknown name → JSON-RPC error -32602.

## Auth & authorization

Two layers, mirroring the `meta_approver_users` pattern from B1
(`corlinman-core/src/config.rs:225-233`):

1. **Connection-level token.** `[mcp.server]` config gains
   `tokens` — a list of opaque bearer strings. The query-string
   `token` must match one entry. Failure → 401 pre-upgrade. Empty
   list (default) means MCP rejects all connections; operators must
   opt in by minting a token. Same fail-closed posture as
   `meta_approver_users = []`.
2. **Per-capability ACL.** Each token entry is a struct, not a bare
   string:

   ```toml
   [[mcp.server.tokens]]
   token = "<opaque-32-byte-base64>"
   label = "claude-desktop-laptop"
   tools_allowlist = ["web_search", "kb.*"]   # glob patterns
   resources_allowed = ["memory", "skill"]    # by URI-scheme prefix
   prompts_allowed = ["*"]
   tenant_id = "default"
   ```

   `tools/list` filters by `tools_allowlist`; `tools/call` rejects
   non-matching with JSON-RPC error -32602 `tool_not_allowed`. Same
   pattern for resources / prompts.

Tenant scoping: `tenant_id` on the token routes the resource
adapter to `TenantPool::pool_for(tenant, "kb"|"persona")`
(`corlinman-tenant/src/pool.rs`). A token without `tenant_id`
defaults to `DEFAULT_TENANT_ID` (`corlinman-tenant/src/id.rs:35`).
Wrong tenant → empty list, never a cross-tenant leak.

No cookie / Basic-auth fallback: the admin Basic-auth path
(`admin_auth.rs:125-177`) is human-shaped; reusing it would conflate
operator login with MCP client identity.

## Test matrix

| Test | Layer | Asserts |
|---|---|---|
| `initialize_returns_advertised_capabilities` | server | `initialize` returns `{tools:{}, resources:{}, prompts:{}}` and `serverInfo.name == "corlinman"` |
| `initialize_then_initialized_promotes_session_state` | session | Out-of-order `tools/list` before `initialize` → -32002 `session_not_initialized` |
| `tools_list_filters_by_allowlist` | tools adapter | Token with `tools_allowlist=["a"]` sees only `a`-prefixed tools |
| `tools_call_dispatches_to_plugin_runtime` | tools adapter | Stub `PluginRuntime` records the `PluginInput`; `CallResult.content[0].text` matches the stub output |
| `tools_call_unknown_returns_jsonrpc_method_not_found` | dispatch | `tools/call { name: "bogus.x" }` → -32601 |
| `tools_call_runtime_error_returns_isError_not_jsonrpc_error` | tools adapter | Runtime returns `Error` → `CallResult.is_error == true`, JSON-RPC frame is `Result`, not `Error` |
| `resources_list_paginates_with_cursor` | resources adapter | Two pages of 100 entries each; second page cursor is server-issued |
| `resources_read_skill_returns_body_markdown` | resources adapter | `corlinman://skill/foo` → `body_markdown` verbatim |
| `resources_read_memory_uses_correct_tenant_pool` | resources adapter | Token `tenant=alpha` only sees alpha's memory hits |
| `prompts_get_unknown_returns_jsonrpc_invalid_params` | prompts adapter | -32602 with the offending name in `data` |
| `auth_missing_token_rejects_pre_upgrade_with_401` | transport | `GET /mcp` without token → 401 (no WS upgrade) |
| `auth_wrong_token_rejects_pre_upgrade_with_401` | transport | wrong token → 401, distinguishable from valid+initialize-fail |
| `disconnect_cancels_inflight_tool_call` | session | Client closes mid-`tools/call`; runtime's CancellationToken fires within 100ms |
| `concurrent_calls_on_one_session_interleave` | dispatch | Two `tools/call` in flight; results return out-of-order keyed by id |
| `desktop_fixture_replay_handshake_through_call` | e2e | Replay captured Desktop trace; server frames match recorded shape modulo `id`+timestamps |
| `reconnect_with_new_token_starts_fresh_session` | transport | First WS dropped; redial → fresh `SessionState` |
| `oversized_frame_rejected_with_close_code_1009` | transport | Frame > `max_frame_bytes` → WS close 1009 |
| `error_envelope_shape_matches_jsonrpc_2_0` | schema | Round-trip each `JsonRpcError` variant; field order matches spec |

## Config knobs

```toml
[mcp.server]
enabled = true
bind = "127.0.0.1:18791"
allowed_origins = ["http://localhost"]   # WS Origin header check; * disables
max_concurrent_sessions = 8
max_frame_bytes = 1_048_576              # 1 MiB; over → close 1009
inactivity_timeout_secs = 300            # idle WS dropped after 5 min
heartbeat_secs = 20                      # PING interval; mirrors wstool default

# One [[mcp.server.tokens]] block per accepted client.
[[mcp.server.tokens]]
token = "<opaque>"
label = "claude-desktop-laptop"
tenant_id = "default"
tools_allowlist = ["*"]
resources_allowed = ["*"]
prompts_allowed = ["*"]
```

Validation: empty `tokens` when `enabled=true` warns (operator may
be staging); bind collision with `[server.bind]` / `[wstool.bind]`
is fatal. Default bind matches roadmap (`phase4-roadmap.md:368`).

## Open questions for the implementation iteration

1. **`MemoryHost::get(id)` API.** Trait
   (`corlinman-memory-host/src/lib.rs:38-58`) has `query`/`upsert`/
   `delete` — no `get`. `resources/read` needs it. Lean: **extend
   the trait** — `LocalSqliteHost` has a SQL row-by-id path already.
2. **Tool name encoding.** `<plugin>.<tool>` collides if a C2
   mcp-stdio plugin passes through dotted names from upstream.
   Lean: **`:` separator** — MCP names allow it, no
   percent-encoding burden.
3. **Resource subscriptions.** Spec'd; persona / memory mutate
   often. A per-resource event bus is non-trivial. Lean: **defer**;
   C1 advertises `subscribe: false`.
4. **Sampling capability.** `sampling/createMessage` would let
   corlinman delegate completions to Desktop's model. Touches Wave
   4 subagent runtime (4-4C, `phase4-roadmap.md:302`); **out of
   scope C1**.
5. **Streaming tool output.** `PluginRuntime::execute` takes a
   `ProgressSink` (`runtime/mod.rs:101-104`); MCP has
   `notifications/progress`. Lean: **wire it** in iter 5 — cheap
   and cuts latency for slow tools.

## Implementation order — 10 iterations

Each item is one PR's worth of work, testable in isolation, ordered
so each iteration leaves the crate compiling and the existing tests
green.

1. **Crate skeleton + JSON-RPC schema types** — workspace member
   (deps: `serde`, `serde_json`, `async-trait`, `thiserror`);
   `lib.rs` re-exports; `schema.rs` with `JsonRpcRequest/Response/
   Error` + three capability payload modules. No transport, no
   adapters. Tests: serde round-trip every wire type, error envelope
   shape. ~10 unit tests.
2. **`McpError` + JSON-RPC error mapping** — `error.rs`:
   Transport / Auth / SessionNotInitialized / MethodNotFound /
   InvalidParams / Internal; `From<McpError> for JsonRpcError`
   mapping (-32600/-32601/-32602/-32603 + custom -32001/-32002).
   6 unit tests.
3. **`SessionState` state machine** — `Connected → Initialized`;
   method dispatch refuses non-`initialize` while `Connected`. No
   transport. 5 unit tests on valid/invalid transitions.
4. **WebSocket transport + `/mcp` route** — modelled on
   `wstool/server.rs:251-273`. Pre-upgrade token check → 401;
   reader loop dispatches frames to a stub handler returning
   `MethodNotFound`. Tests: 401 paths, 101 + stub MethodNotFound,
   frame size limit. ~6 tests.
5. **`CapabilityAdapter` trait + `tools` adapter** —
   `adapters/tools.rs` wraps `Arc<PluginRegistry>` + the
   `protocol::dispatcher` executor; `tools/list`+`tools/call`;
   bridge `ProgressSink` → `notifications/progress`. Tests: list,
   call success, runtime-error → `is_error`, unknown → -32601,
   allowlist filter. ~8 tests.
6. **`prompts` adapter** — wrap `Arc<SkillRegistry>`. Tests: list,
   get, unknown → -32602, allowlist filter. 5 tests.
7. **`MemoryHost::get(id)` extension + `resources` adapter** —
   open question §1 lands here. Extend trait + `local_sqlite.rs`
   impl; adapter wraps memory host + persona store + skill
   registry; three URI schemes; cursor pagination. Tests: list
   paginates, read each scheme, unknown URI → -32602, tenant
   isolation. ~10 tests.
8. **Auth ACL + tenant scoping** — `server/auth.rs`; tokens parsed
   into `Vec<TokenAcl>`, resolved at pre-upgrade, stamped on
   `SessionContext`. Adapters consult it. Tests: allowlist filter
   at list + call; cross-tenant reads empty; missing tenant
   defaults to `DEFAULT_TENANT_ID`. 6 tests.
9. **Gateway integration — mount `/mcp` + config** — add
   `McpConfig` to `corlinman-core::config` (mirrors `WsToolConfig`
   shape, `config.rs:1297-1313`); `server.rs` builds `McpServer`
   with existing `PluginRegistry` / `MemoryHost` / `SkillRegistry`
   and merges its router. Add `mcp` to `RESTART_REQUIRED_SECTIONS`
   (`config_watcher.rs:56`). 4 integration tests.
10. **E2E against Claude Desktop fixture** — capture a real
    Desktop session (handshake → `tools/list` → `tools/call` →
    `resources/list` → `resources/read` → close); commit the JSON
    trace under `tests/fixtures/desktop_2024_11_05.json`. Replay
    test asserts every server frame matches recorded shape
    (modulo `id` + timestamps). Plus
    `cargo run --example mcp-cli-smoke` for ad-hoc debugging
    against the spec's reference client.

## Out of scope (C1)

- **`kind = "mcp"` plugin adapter** — outbound client, lands in
  C2 (`phase4-roadmap.md:281`). C1's `schema.rs` + `error.rs` are
  the shared substrate; C2 adds `client::*`.
- **`resources/subscribe`** — change notifications. C1 advertises
  `subscribe: false`. See Open question §3.
- **`sampling/createMessage`** — touches Wave 4 subagent design
  (4-4C); revisit then.
- **stdio transport** — corlinman is a long-lived service, not a
  child process. A future stdio shim is a thin wrapper around
  `transport.rs` if needed.
- **HTTP+SSE fallback** — revisit only if a corp proxy blocks WS.
- **Token issuance / revocation admin UI** — C1 reads tokens from
  config; operators edit `corlinman.toml` + SIGHUP. C2-or-later
  ships a `/admin/mcp/tokens` route.
- **Browser-extension surface** — the `phase4-roadmap.md:314`
  stretch goal reuses C1's endpoint; no separate work here.
