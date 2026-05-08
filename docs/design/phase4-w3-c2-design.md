# Phase 4 W3 C2 — MCP plugin adapter (consume any stdio MCP server as a corlinman tool)

**Status**: Design (pre-implementation) · **Owner**: TBD · **Created**: 2026-05-08 · **Estimate**: 5-7d

> C1 turns corlinman into an MCP **server** (anyone can wire corlinman
> as a tool source into Claude / Cursor / Continue). C2 inverts the
> arrow: corlinman becomes an MCP **client**, so any
> `npx`/`uvx`-published MCP server (filesystem, github, slack,
> postgres, …) registers as a native corlinman tool by dropping a
> `plugin-manifest.toml` with `plugin_type = "mcp"`. One manifest
> kind, dozens of free tools.

Design seed for the iteration sequence below. Pins the manifest v3
shape, the spawn / handshake / shutdown lifecycle, the
sandbox-or-not decision, the env-passthrough redaction story, and
the v2→v3 in-memory migration. Mirrors `phase4-w2-b1-design.md` in
shape and depth. Assumes
[`phase4-w3-c1-design.md`](./phase4-w3-c1-design.md) has landed and
that the `corlinman-mcp` crate already exposes
`corlinman_mcp::client::McpClient::connect_stdio(cmd, args, env)`
plus `tools_list() / tools_call(name, args)` (same `Tool` /
`CallToolResult` types both sides share).

## Why this exists

The plugin ecosystem the team will *not* write itself is
already published — `@modelcontextprotocol/server-filesystem`,
`@modelcontextprotocol/server-github`, `mcp-server-postgres`,
`mcp-server-slack`, plus a long tail on PyPI / npm. Each is one
`npx`/`uvx` invocation away. The alternative — a hand-rolled
`corlinman-channel-github` for every one of them — duplicates work
the MCP community already maintains and re-implements the same wire
protocol three times over. C2 takes the position that the cheapest
way to land 20 new tools is a single adapter, not 20 native plugins.

The pre-existing surface is friendly:
`corlinman-plugins/src/manifest.rs:117-127` already names the
runtime taxonomy `Sync | Async | Service`; the registry
(`registry.rs:30-46`) already keys on plugin name; the dispatcher
(`gateway/src/routes/chat.rs:531-599`) already routes by
`PluginType`. Adding a fourth runtime variant — `Mcp` — is the
small change. Translating MCP `tools/list` into the same
`Capabilities.tools` vector the registry already exports is the
medium change. Everything else (admin API, hot reload, dispatch,
metrics) reuses what B1 / M6 already shipped.

## Manifest v3 — the `mcp` table

One new variant in `PluginType`
(`corlinman-plugins/src/manifest.rs:117`) plus one new optional
table at the manifest root. The TOML stays the same shape as v2
manifests; only `plugin_type = "mcp"` is novel:

```toml
manifest_version = 3
name = "fs"
version = "0.1.0"
description = "Filesystem access via @modelcontextprotocol/server-filesystem"
plugin_type = "mcp"

[entry_point]                              # reused; no new field
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/data"]

[mcp]                                      # NEW (only present when plugin_type = "mcp")
autostart = true                           # spawn at gateway boot vs lazy on first call
restart_policy = "on_crash"                # "never" | "on_crash" | "always"
crash_loop_max = 3                         # mirrors supervisor.rs:34
crash_loop_window_secs = 60
handshake_timeout_ms = 5000                # MCP `initialize` round-trip deadline
idle_shutdown_secs = 0                     # 0 = keep alive forever; >0 = shut down after N idle secs

[mcp.env_passthrough]                      # which gateway env vars get forwarded to the child
allow = ["GITHUB_TOKEN", "OPENAI_API_KEY"] # exact-name allowlist; nothing else leaks
deny  = ["AWS_*"]                          # glob deny over the allow set (defence-in-depth)

[mcp.tools_allowlist]                      # filter MCP `tools/list` before exporting
mode = "allow"                             # "allow" (default) | "deny" | "all"
names = ["read_file", "list_directory"]    # exact tool names from the upstream MCP server

[mcp.resources_allowlist]                  # filter MCP resources/* (deferred to iter 9)
mode = "deny"
patterns = ["file:///etc/**", "file:///root/**"]

[capabilities]                             # AUTO-POPULATED at register time, not authored
# tools = []  -- the loader rewrites this from `tools/list` after handshake.
disable_model_invocation = false
```

**Why a `[mcp]` table instead of nesting under
`[entry_point]`**: the entry point is shared with sync/async/service
(`manifest.rs:141-156`). The MCP-specific knobs (handshake timeout,
allowlist, restart policy) are orthogonal to "how do I exec this
binary" and conflating them muddles validation. Strict
`deny_unknown_fields` on each table (already the convention) means
authoring an `[mcp]` block on a non-MCP plugin is a hard parse
error, not a silent ignore.

**`tools_allowlist`** vs trusting the upstream server: an MCP server
typically exposes everything; the operator wants the tool surface
*they* approved, not whatever the upstream maintainer ships next
release. `mode = "allow"` (default) and an empty `names` means **no
tools exported** — fail-closed. `mode = "all"` is the explicit opt-in
escape hatch.

**`capabilities.tools` is auto-populated** rather than authored:
duplicating tool schemas in two places (manifest + upstream MCP
server) drifts. The registry rewrites `capabilities.tools` after the
handshake using `McpClient::tools_list()`, mapping each MCP `Tool`
into a manifest `Tool` (`manifest.rs:184-201`) — same JSON-Schema
shape, so the existing dispatcher needs zero changes to validate
arguments.

## Lifecycle — spawn → handshake → idle → call → reload → terminate

```
                +-----------+   spawn        +--------------+
                | Registered|--------------->| Spawning     |  cmd / args / scoped env
                +-----------+                +------+-------+
                      ^                             |
              reload  |                             | initialize  (MCP handshake)
                      |                             v
              +-------+----+   tools/list    +--------------+
              | Healthy    |<----------------|  Initialized  |
              +-+-----+----+                 +--------------+
                |     |                            ^
       call x N |     | child exited / sigchld     |
                v     |                            |
              +-------+----+                       |
              |  Idle      |---- idle_shutdown ----+ (or restart_policy)
              +------------+
                      |
                      | tear-down (gateway shutdown / admin remove)
                      v
                  +--------+
                  | Stopped |
                  +--------+
```

**Spawn**: a new `McpRuntime` (sibling to
`runtime/jsonrpc_stdio.rs` and `runtime/service_grpc.rs`) calls
`McpClient::connect_stdio(cmd, args, scoped_env)`. `cwd` is the
manifest dir, mirroring sync/async semantics
(`runtime/jsonrpc_stdio.rs:1-30`). `kill_on_drop(true)` is enforced
(same pattern as `supervisor.rs:115`) so a panic in the gateway
doesn't orphan child processes.

**Handshake**: the MCP `initialize` request is sent and awaited
under `mcp.handshake_timeout_ms`. On timeout the child is killed and
the registry transitions to `Failed { reason }`; admin
`/admin/plugins/:name` surfaces the cause without a gateway restart.

**Tools list**: immediately after `initialize`, `tools/list` is
called once. The result is filtered through `tools_allowlist` and
written into `PluginEntry.manifest.capabilities.tools`. A subtle
choice: we wrap the existing `Arc<PluginManifest>` in a
`McpAdapterCell` that owns the **resolved** tools so we don't have to
mutate the inner `Arc` (which is also handed to the read-only admin
view at `routes/admin/plugins.rs:42-65`). The cell is consulted on
dispatch; the manifest stays immutable.

**Idle**: with `autostart = true` and `idle_shutdown_secs = 0` the
child stays alive across calls — typical for stateful servers
(filesystem, github auth). With `idle_shutdown_secs = N > 0` the
adapter starts a tokio interval; after N idle seconds it issues a
graceful close (`stdin.shutdown()`) then waits for child exit.

**Call**: a `tools/call` MCP request multiplexed over the *same*
stdio connection (the MCP protocol is bidirectional JSON-RPC, unlike
our existing `jsonrpc_stdio` runtime which is one-shot
spawn-per-call — `runtime/jsonrpc_stdio.rs:6-13`). One outstanding
`call_id`-keyed oneshot per request; responses route by id. Reusing
the connection is essential — many MCP servers cache state per
session and the `npx -y` cold-start is 1-3s.

**Reload**: when the watcher (`registry/watcher.rs` from M6) sees a
manifest edit, the adapter sends `stdin.shutdown()`, waits for exit
(2s deadline before SIGKILL), then re-spawns. Reload is a **no
in-flight calls** transition — pending `tools/call` futures get
`PluginRuntime::Cancelled` errors.

**Terminate**: gateway shutdown calls `McpAdapter::stop_all()`,
mirroring `supervisor.rs:154-161`.

**Failure handling**: a child exit while the adapter is in `Healthy`
or `Idle` triggers `restart_policy`. `on_crash` schedules respawn
with backoff `[1s, 2s, 5s, 10s]` (mirrors
`supervisor.rs:38-43`); `crash_loop_max=3` inside
`crash_loop_window_secs=60` flips the entry to `Failed` and stops
trying. `never` lets the entry sit dead — admin must hit
`POST /admin/plugins/:name/restart` to revive it.

## Sandbox integration — process-level only for C2

The roadmap proposes a `corlinman-sandbox` crate
(`phase4-roadmap.md:325`) but that crate does not yet exist (read:
no Wave 1/2 work landed it). The current sandbox surface lives
inside `corlinman-plugins/src/sandbox/` and is **Docker-only**
(`sandbox/mod.rs:1-15`, `sandbox/docker.rs`), enabled when any
`[sandbox]` field is set (`sandbox/mod.rs:29-36`).

**Decision**: C2 ships **without containerisation by default**. An
`npx`-spawned MCP server runs as a child process under the gateway
user, with three real protections instead of one fake one:

1. **Env scoping** — only `mcp.env_passthrough.allow` keys reach the
   child. Unlike the `sync` runtime which passes the full inherited
   env, the MCP runtime starts from a *blank* env and copies only
   allowlisted names. This is the single biggest source of
   accidental secret leakage and is cheap to get right.
2. **Working directory** — `cwd` is the manifest dir, same as
   `runtime/jsonrpc_stdio.rs`. No `$HOME` expansion in
   `entry_point.args` (we already don't shell-expand —
   `manifest.rs:148`).
3. **Optional Docker reuse** — the existing `[sandbox]` block is
   still honoured: a manifest declaring `plugin_type = "mcp"` *and*
   `[sandbox] memory = "256m"` runs the MCP child inside the
   existing `DockerSandbox` (`sandbox/docker.rs`). The MCP stdio
   transport pipes through `docker run -i` exactly as the sync
   runtime already does for stdin/stdout. Re-using rather than
   forking the sandbox layer means C2 inherits docker hardening for
   free without designing a second one.

**Why not require Docker**: most MCP servers in the wild expect
filesystem access (filesystem, github clone, postgres unix sockets).
Forcing Docker would make the 80% case (operator runs `npx` MCP
server on their own data) painful, and the 20% case (untrusted MCP
server) is *better served* by the dedicated `corlinman-sandbox`
crate the roadmap promises in Wave 4. C2 is explicitly the
"cheap-tools-now" milestone; hardening is D-wave.

## Adapter layer — translating MCP into the corlinman tool ABI

Three translation seams:

| Direction | corlinman side | MCP side | Notes |
|---|---|---|---|
| Tool schema | `manifest::Tool { name, description, parameters: serde_json::Value }` (`manifest.rs:184-197`) | `mcp::Tool { name, description, input_schema }` | 1:1; `input_schema` is JSON Schema in both. The shared types from `corlinman-mcp` (C1) avoid a copy. |
| Argument call | `PbToolCall { tool, args_json: Bytes, call_id }` (chat dispatcher) | `tools/call { name, arguments }` JSON-RPC | `args_json` parses to `serde_json::Value`, becomes `arguments`; `call_id` is the JSON-RPC id. |
| Response | `PluginOutput::Success { content: Bytes }` (`runtime/mod.rs:46-55`) | `CallToolResult { content: Vec<Content>, is_error: bool }` | `is_error == true` becomes `PluginOutput::Error { code: -32603, message: <flatten content> }`; success `content` is JSON-encoded back into the `Bytes` payload. |

**Streaming**: MCP supports `notifications/progress`. The adapter
emits these via the existing
`runtime::ProgressSink` trait (`runtime/mod.rs:101-104`) — same
sink the JSON-RPC stdio runtime uses today. Token by token streaming
of tool *output* is **not** in the MCP spec for `tools/call` (only
notifications), so streaming response bodies stays out-of-scope.

**Tool name namespacing**: MCP plugins inherit the same
`<plugin>.<tool>` pattern that `manifest.rs:174` already documents.
A filesystem MCP server registered as `fs` exports
`fs.read_file`, `fs.list_directory`. Collisions with native plugins
are caught at the existing registry dedup
(`registry.rs:207-243`).

## Registry surface — runtime add / remove / disable

The current admin surface (`routes/admin/plugins.rs:75-95`) is
read-only. C2 adds three mutations behind the existing operator-only
admin auth:

```text
POST /admin/plugins                       body: { manifest_toml: "..." }
  → 201 { name, status: "installed" }       writes plugin-manifest.toml under
                                            $CORLINMAN_DATA_DIR/plugins/<name>/
                                            and triggers HotReloader.upsert.

DELETE /admin/plugins/:name
  → 200 { stopped: true, removed: true }    stops MCP child, removes manifest dir.

POST /admin/plugins/:name/disable
  → 200 { disabled: true }                  registry sets entry.disabled = true; child
                                            terminates; tools/list omits the entry.
```

`disabled` is a new field on `PluginEntry` (`registry.rs:31-38`)
persisted by writing a `.disabled` sentinel file alongside the
manifest, so the state survives gateway restart without a second
config file. The hot reloader (`registry/watcher.rs`) already
notices file changes; it learns one new sentinel.

## Auth / secrets — env passthrough without leaking

Three rules, in order:

1. **Allowlist-only**, never blacklist-only: `env_passthrough.allow`
   is the canonical surface. An empty allow list means **no env
   vars** reach the child — minus `PATH`, `HOME`, `USER`, `LANG`
   (the four required for `npx` / `uvx` to even start, which the
   adapter prepends unconditionally).
2. **Deny is a defence-in-depth filter** over the allow set: if a
   future operator adds `*` to allow, `deny = ["AWS_*"]` still keeps
   the AWS keys out. Glob matching is `globset`, same pattern the
   evolution applier already uses.
3. **Log redaction**: the value of any allowlisted env var that
   matches `*_TOKEN | *_KEY | *_SECRET | *_PASSWORD` (case-insensitive)
   is redacted to `[REDACTED:<sha256[..8]>]` in every log line. The
   redaction salt is per-process so leaked log lines don't decode
   back. `tracing` field redaction lives in a tiny
   `corlinman-plugins/src/mcp/redact.rs` helper rather than a
   global filter — surgical and testable.

The MCP child never sees raw secret values **in argv** (the existing
`entry_point.args` field stays string-only and is logged as-is); to
inject a token, the operator references it via the env, e.g. `args
= ["--token-from-env", "GITHUB_TOKEN"]`. The MCP adapter does not
substitute env into args — keeps the redaction story honest.

## Manifest v3 migration — backward compat with v2

Three rules:

1. **`MAX_SUPPORTED_MANIFEST_VERSION` bumps to 3**
   (`manifest.rs:114`). v2 manifests continue to load; v1 manifests
   continue to migrate to v2 in memory
   (`manifest.rs:262-274`).
2. **`migrate_to_v2_in_memory` becomes
   `migrate_to_current_in_memory`** with a v2→v3 step that's a no-op
   for non-MCP plugins: bump `manifest_version` to 3, leave
   everything else alone. The function stays idempotent
   (`manifest.rs:264`).
3. **`PluginType::Mcp` parses only when `manifest_version >= 3`**.
   Loading `plugin_type = "mcp"` with `manifest_version = 2`
   returns a validation error suggesting the bump — v3 is required
   so an old gateway loading a v3 manifest fails *loudly*
   (`manifest.rs:281-287`) instead of treating `mcp` as an unknown
   string (which would deserialise-fail on the enum anyway, but the
   version check gives a better diagnostic).

The on-disk file is **not rewritten** — same policy as v1→v2
(`manifest.rs:259-262`). A user authoring a v3 manifest stamps `3`
themselves; the loader respects what's on disk.

## Test matrix

| Test | Layer | Asserts |
|---|---|---|
| `mcp_kind_parses_under_v3` | manifest | `plugin_type = "mcp"` + `manifest_version = 3` parses; `manifest_version = 2` rejects with version-bump hint |
| `mcp_table_unknown_field_rejected` | manifest | `[mcp] mystery = 1` is a parse error (deny_unknown_fields) |
| `tools_allowlist_default_is_fail_closed` | manifest | `mode = "allow"` + empty `names` → zero tools exported |
| `env_passthrough_strips_unallowlisted` | adapter | `GITHUB_TOKEN` allowed → forwarded; `AWS_KEY` denied → absent in child env |
| `env_passthrough_redacts_in_logs` | adapter | tracing capture: token value never appears verbatim; `[REDACTED:<8 hex>]` placeholder present |
| `spawn_handshake_list_call_shutdown_happy_path` | E2E | against `@modelcontextprotocol/server-filesystem`: spawn, init, list 3 tools, call `read_file`, graceful shutdown |
| `handshake_timeout_kills_child_and_marks_failed` | adapter | server that ignores `initialize` for >5s → child SIGKILLed, entry status = `failed` |
| `crash_during_idle_respawns_with_backoff` | adapter | kill child in idle state → 1s backoff, re-spawn, re-handshake; metric `mcp_restarts_total` increments |
| `crash_loop_max_stops_respawn` | adapter | 3 crashes in 60s → entry transitions to `failed`; no further spawn for 60s |
| `tools_list_filtered_by_allowlist` | adapter | upstream returns 5 tools; allowlist=2 → registry exports 2; rejected ones absent from `/admin/plugins/:name` |
| `concurrent_tool_calls_multiplex_correctly` | adapter | 8 concurrent `tools/call` against same child → all return correct results, ids never crossed |
| `reload_during_inflight_call_returns_cancelled` | adapter | manifest edit while a call is mid-flight → call resolves to `Cancelled`, child re-spawns, next call succeeds |
| `disable_then_enable_persists_across_restart` | registry | `POST /disable` writes `.disabled` sentinel; gateway restart → entry stays disabled until `POST /enable` |
| `v2_to_v3_migration_round_trip` | manifest | v2 manifest loads under v3 loader; `manifest_version` reads back as 3 in memory but file on disk untouched |
| `v3_only_field_on_v2_manifest_rejected` | manifest | `manifest_version = 2` + `[mcp]` table → validation error |
| `docker_sandbox_wraps_mcp_child` | adapter+docker | manifest with both `plugin_type = "mcp"` and `[sandbox] memory = "256m"` runs MCP server inside `docker run -i`; stdio still pipes |

## Config knobs

`[plugins]` is implicit today — there's no top-level config block
for plugins, only the `CORLINMAN_DATA_DIR` / `CORLINMAN_PLUGIN_DIRS`
env vars (`gateway/src/server.rs:691-702`). C2 introduces a small
section in `corlinman.toml` for adapter-wide knobs that don't belong
in any single plugin manifest:

```toml
[plugins.mcp]
default_handshake_timeout_ms = 5000
default_idle_shutdown_secs = 0
default_restart_policy = "on_crash"
log_child_stderr = true        # capture MCP child stderr into gateway tracing
allow_docker_sandbox = true    # operator kill-switch for the sandbox path
```

These act as **defaults** for fields the manifest may override.
Existing manifests with explicit `[mcp]` values are unchanged.

## Open questions

1. **Resource subscriptions**: MCP supports `resources/subscribe` for
   change notifications. Out of scope for C2 (we land `tools/*` only)
   but if `resources_allowlist` ships in v3, the field shape needs to
   not lock us out. Recommendation: ship as documented but no
   adapter implementation — flagged `unimplemented!()` if the
   manifest sets it. Iter 9 maybe; Wave 4 D-task more likely.
2. **Per-tenant MCP plugins**: C1 / C2 land plugins as gateway-global
   today (registry is a single map — `registry.rs:79-86`). Multi-tenant
   isolation arrives with the `corlinman-tenant` work
   (`phase4-roadmap.md:316`). For C2 we leave the per-tenant question
   to Wave 4 D and document that an MCP plugin's secrets (env) leak
   across tenants by default.
3. **Streaming partial results**: a long-running MCP tool call
   (e.g. recursive grep over a large repo) blocks until completion.
   `notifications/progress` lands incremental progress numbers but
   not partial output. Acceptable trade-off for C2; revisit if
   operators ask.
4. **Schema drift on hot reload**: if the upstream MCP server's
   `tools/list` changes between spawns (e.g. server upgrade), an
   in-flight `tools/call` from a stale schema can fail. Recommendation:
   on re-handshake, diff old vs new tool set; warn-log added /
   removed; reject calls referencing removed tools with
   `tool_no_longer_available`.

## Implementation order — 10 iterations

Each numbered item is a single bounded iteration (~30 min - 2 hours):

1. **Manifest v3 schema in `corlinman-plugins`** — add `PluginType::Mcp`
   variant to `manifest.rs:117`; bump
   `MAX_SUPPORTED_MANIFEST_VERSION` to 3; add the `Mcp` table struct
   (`McpConfig` with `autostart`, `restart_policy`,
   `crash_loop_*`, `handshake_timeout_ms`, `idle_shutdown_secs`,
   `env_passthrough`, `tools_allowlist`, `resources_allowlist`);
   wire `migrate_to_current_in_memory`. Tests:
   `mcp_kind_parses_under_v3`, `v3_only_field_on_v2_manifest_rejected`,
   `tools_allowlist_default_is_fail_closed`, `mcp_table_unknown_field_rejected`.
2. **Stdio spawn + reap primitive** — new
   `corlinman-plugins/src/runtime/mcp_stdio.rs` (sibling to
   `jsonrpc_stdio.rs`). Wraps `corlinman_mcp::client::McpClient::connect_stdio`.
   `kill_on_drop(true)`, scoped env helper, `cwd` to manifest dir.
   No registry wiring yet. Tests: spawn `cat` → write → read EOF;
   spawn missing binary → error path.
3. **Env passthrough + redaction helper** — new
   `runtime/mcp/redact.rs`; allow/deny + glob filter; sensitive-name
   redaction for tracing fields. Tests:
   `env_passthrough_strips_unallowlisted`, `env_passthrough_redacts_in_logs`.
4. **Adapter struct + handshake** — `McpAdapter` owns one
   `McpClient` per registered MCP plugin; runs `initialize` under
   `handshake_timeout_ms`. State machine
   (`Spawning | Initialized | Healthy | Idle | Failed | Stopped`).
   Tests: handshake happy path; handshake timeout kills child.
5. **Tools list + filter** — `tools_list()` after handshake;
   apply `tools_allowlist`; rewrite the resolved tools into a
   `McpAdapterCell` keyed by plugin name. Tests:
   `tools_list_filtered_by_allowlist`.
6. **PluginRuntime trait impl** — `McpRuntime` implements
   `PluginRuntime` (`runtime/mod.rs:77-98`); `tools/call`
   multiplexed via the live `McpClient`; `ProgressSink` wired. Tests:
   call → success path; call → MCP error path; concurrent calls.
7. **Dispatcher branch** — `routes/chat.rs:561` gains a `PluginType::Mcp`
   arm that resolves the adapter from a new
   `AppState.mcp_adapter: Arc<McpAdapter>`. Default state
   construction (`state.rs:115`) supplies a no-op adapter. Tests:
   `mcp_dispatch_routes_to_adapter` (integration).
8. **Crash + restart supervisor** — child-exit watcher per plugin;
   backoff schedule reuses `supervisor.rs:38-43`; crash-loop ceiling
   transitions to `Failed`. Tests: `crash_during_idle_respawns_with_backoff`,
   `crash_loop_max_stops_respawn`.
9. **Admin mutations** — `POST /admin/plugins`, `DELETE /admin/plugins/:name`,
   `POST /admin/plugins/:name/disable|enable|restart` in
   `routes/admin/plugins.rs`; `.disabled` sentinel persistence;
   hot-reloader integration. Tests:
   `disable_then_enable_persists_across_restart`,
   `add_then_remove_round_trips`.
10. **E2E against a real MCP server** — gated integration test
    spawning `npx -y @modelcontextprotocol/server-filesystem /tmp`
    behind a `requires_npx` cargo feature so CI without node skips it.
    Asserts: spawn → list 3 tools → call `read_file` on a fixture →
    receive expected bytes → graceful shutdown. Single test,
    keeps the real-world claim honest.

## Out of scope (C2)

- **Non-stdio transports** (sse, http) — deferred. C1 already lands
  the SSE *server* surface; the *client* SSE adapter is a separate
  task in Wave 4 because the auth story (per-server bearer tokens,
  TLS cert pinning) needs more thought than the stdio case.
- **Sandbox hardening beyond Docker** — the `corlinman-sandbox` crate
  promised at `phase4-roadmap.md:325` doesn't exist yet. C2 reuses
  the existing Docker integration if a manifest opts in; gVisor /
  firecracker / wasmtime are Wave 4 D-task.
- **Per-tenant MCP plugins** — see open question 2. The registry is
  gateway-global today; tenant scoping is multi-task federation work.
- **MCP `resources/*` and `prompts/*` sides of the protocol** — only
  `tools/*` lands. The manifest reserves
  `mcp.resources_allowlist` so we can add resource support without a
  v4 bump, but the adapter rejects with `unimplemented` if a manifest
  sets it.
- **Auto-installing MCP servers** — `POST /admin/plugins` accepts a
  manifest pointing at an existing `npx` invocation; it does **not**
  run `npm install -g` for the operator. Provisioning the binary is
  the operator's job; the adapter only consumes.
- **Discovery from a public MCP registry** — out of scope; the
  ecosystem doesn't have an authoritative one yet.

---

## 100-word summary

C2 turns `corlinman-plugins` into an MCP client: any stdio MCP
server (filesystem, github, postgres, …) becomes a corlinman tool by
dropping a `plugin-manifest.toml` with `plugin_type = "mcp"`. We
add a v3 manifest with an `[mcp]` table (handshake timeout, restart
policy, env allowlist, tool allowlist) and a new `McpRuntime`
sibling to the existing stdio / gRPC runtimes. Sandbox is
process-level by default and reuses the existing Docker layer when
the manifest opts in. Backward compat: v1/v2 manifests continue to
load; only `plugin_type = "mcp"` requires v3. Ten iterations land
spawn → handshake → multiplexed call → crash recovery → admin
mutations → real MCP-server E2E.
