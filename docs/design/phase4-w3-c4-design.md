# Phase 4 W3 C4 — Reference Swift macOS client

**Status**: Design (pre-implementation) · **Owner**: TBD · **Created**: 2026-05-08 · **Estimate**: 7-10d

> A minimal SwiftUI macOS app under `apps/swift-mac/` that drives the
> gateway end-to-end: streamed chat, multi-tenant auth, persistent
> session list, tool-approval modal, and push notifications when long-
> running work completes. Not a shipping product. Its job is to **pin
> the contract** so iOS and Android teams can copy a working pattern
> instead of reverse-engineering it from `routes/chat.rs`.

This is the first non-web client. Every fuzzy edge in the wire
protocol — keep-alive cadence, SSE framing of tool_calls, approval
round-trip shape, push payload schema — gets exercised. When the Swift
client works against a real gateway, the contract is real; until then,
the contract is whatever the TypeScript UI happens to do.

## Why this exists

Phase 4 Wave 3 (`phase4-roadmap.md:285,427`) calls for a reference
client that proves out the native-surface contract. Today the gateway
serves `/v1/chat/completions` over HTTP+SSE
(`rust/crates/corlinman-gateway/src/routes/chat.rs:1-22`) and speaks
gRPC **internally** to the Python agent
(`proto/corlinman/v1/agent.proto:21-23`). No external client speaks
gRPC; no external client receives push notifications.

Three things this delivers that no other client demonstrates:

1. **Streamed reasoning UX** — token deltas + tool-call deltas + tool-
   approval interleave, all on one channel, all rendered live. The
   web UI exercises this; a native Mac client forces the same flow
   to work without browser SSE niceties (no `EventSource`, no
   automatic reconnect).
2. **Push when the user is away** — chat is interactive, but skill
   evolution / scheduler runs / approval needs are async. Today QQ
   and Telegram are the only out-of-band notification surfaces
   (`rust/crates/corlinman-channels/src/lib.rs:8-13`). Native macOS
   gets a third: APNs in prod, a stub socket in dev.
3. **Auth + tenant selection at first launch** — credential capture,
   Keychain storage, tenant picker against the live admin DB. Mirrors
   what an iOS app will need on first install.

## Scope

The Mac app **does**:

- Send a user message → stream the response token-by-token into a
  scrolling chat view (mirrors the SSE delta protocol at
  `chat.rs:1-22`, which the client consumes verbatim — no intermediate
  proxy).
- Persist the session list locally (SQLite via GRDB), keyed by
  `session_key`. On launch, show the last N sessions; selecting one
  resumes the conversation by re-sending its history with
  `session_key` set.
- Receive a push notification when a long-running task completes
  (skill evolution applied, approval queue grew, scheduler run
  finished). APNs in prod. In dev, a Unix-domain socket the gateway
  writes to and the app reads from — same payload schema, no Apple
  account required.
- Surface a tool-approval modal when the agent emits an
  `AwaitingApproval` frame (`agent.proto:137-143`). Operator picks
  approve / deny + scope (`once`/`session`/`always`); the decision
  goes back as `ApprovalDecision` (`agent.proto:99-106`) — but
  through the gateway's HTTP relay, not the raw gRPC stream.
- Multi-tenant: first-launch onboarding asks for gateway URL,
  username, password, tenant slug. Tenant selector in the toolbar
  for operators with access to multiple tenants.

The Mac app **does not**:

- Voice input or audio output. That is W4 D4
  (`phase4-roadmap.md:303`); a separate stack (whisper / TTS) the
  reference client deliberately avoids so the chat contract stays
  the focal point.
- MCP server / client integration. C1 + C2 lay down the MCP surface;
  this client uses the same chat path mortals use. An MCP-aware
  reference client is a future iteration.
- Admin / agent management surfaces. The web UI under `ui/` already
  covers `/admin/*`; duplicating it in SwiftUI is busy-work without
  signal. The Mac app is a **chat client**, not an admin console.
- iOS or iPadOS shipping. The codebase is shaped to make iOS-port a
  small step (see Architecture), but builds + signs only macOS.

## Architecture

New top-level directory `apps/swift-mac/`. Sibling to `python/`,
`rust/`, `ui/` — outside the Cargo workspace, outside the npm
workspace.

```
apps/swift-mac/
├── Package.swift               # SwiftPM manifest, target wiring
├── README.md                   # quickstart: build, run, point at gateway
├── .gitignore                  # .build/, .swiftpm/, *.xcuserstate
├── Sources/
│   ├── CorlinmanProto/         # generated swift-protobuf bindings (one file per .proto)
│   │   └── (build-time generated, .gitignored)
│   ├── CorlinmanCore/          # gateway client, auth, persistence
│   │   ├── GatewayClient.swift          # HTTP+SSE chat + admin REST calls
│   │   ├── ChatStream.swift             # AsyncSequence<ChatChunk> over SSE
│   │   ├── AuthStore.swift              # Keychain wrapper
│   │   ├── SessionStore.swift           # GRDB-backed local session cache
│   │   ├── PushReceiver.swift           # APNs delegate + dev-socket fallback
│   │   └── Models.swift                 # ChatRequest/Response/ApprovalDecision
│   ├── CorlinmanUI/            # SwiftUI views — pure, no networking
│   │   ├── ChatView.swift               # message list + composer
│   │   ├── SessionListView.swift        # sidebar
│   │   ├── ApprovalSheet.swift          # tool-approval modal
│   │   ├── OnboardingView.swift         # first-launch credential capture
│   │   └── Theme.swift                  # colors, fonts (mirror ui/ tokens later)
│   └── CorlinmanApp/           # @main entry point + dependency wiring
│       └── CorlinmanApp.swift
└── Tests/
    ├── CorlinmanCoreTests/     # codec, mock-server integration, push handler
    └── CorlinmanUITests/       # snapshot tests for ChatView + ApprovalSheet
```

Three SwiftPM targets — `CorlinmanCore`, `CorlinmanUI`, `CorlinmanApp`
— with `CorlinmanApp` depending on the other two. The split is not
ceremony: it forces UI views to consume `Core` types through
protocols, which is what makes the same code recompile for iOS later
(swap `CorlinmanApp` for an iOS app target; reuse `Core` and most of
`UI`).

### Wire choice — HTTP+SSE first, gRPC where it earns its keep

The roadmap says "gRPC bindings to gateway"
(`phase4-roadmap.md:285`), but the gateway's chat surface today is
HTTP+SSE (`chat.rs:1-22`). gRPC lives between gateway and Python
(`agent.proto:21-23`). Two options:

| Option | Pros | Cons |
|---|---|---|
| Swift client speaks HTTP+SSE to `/v1/chat/completions` | Zero gateway change; OpenAI-compat path; works through proxies + curl | SSE on macOS needs custom URLSession framing (Apple has no built-in) |
| Gateway grows a public `Agent.Chat` gRPC surface; Swift uses gRPC-Swift | Closer to the canonical contract; bidir streaming for free | New attack surface, new auth wiring, gRPC-Swift's macOS story is still rough vs Connect-Swift, and the **other** client (web) doesn't use it |

**Decision**: HTTP+SSE for chat (path of least resistance, matches the
existing TypeScript client surface), keep `swift-protobuf` for
**payload schemas only** (`Attachment`, `ToolCall`, `ApprovalDecision`
shapes — see `agent.proto:71-106`). The roadmap line is satisfied by
"the client uses the same protobuf message definitions the gateway
uses internally" — without forcing the gateway to publish a brand new
gRPC surface. Open question §1 revisits whether to grow that surface
later.

## gRPC / proto bindings — codegen workflow

`swift-protobuf` only — no gRPC stubs needed (chat is HTTP+SSE; the
proto types are used as **JSON-encodable models** that mirror the
wire shapes in `agent.proto` and `common.proto`). Two integration
options for codegen:

1. **SwiftPM build plugin** (`.binaryTarget` of `protoc-gen-swift`).
   No external script; clean dev experience. Bootstrap cost: pin the
   plugin version in `Package.swift`; CI installs nothing extra.
2. **Pre-build script** (`scripts/regen_proto.sh`) that calls `protoc`
   with `--swift_out` and commits the generated `.pb.swift` files.
   Less Swift-native but cross-platform; matches how
   `corlinman-proto/build.rs` works for Rust.

**Decision**: SwiftPM build plugin (option 1). Generated files stay
in `.build/` and are git-ignored — keeps the repo lean and ensures
schema drift fails the build instead of silently using stale
generated code. CI installs `swift` 5.10+, no separate `protoc`.

Build dependency from `apps/swift-mac/`'s `Package.swift` resolves
the proto source files at `../../proto/corlinman/v1/*.proto` —
same root used by the Rust build script
(`rust/crates/corlinman-proto/build.rs:11-22`). One source of truth,
two language clients regenerate from the same files.

## Auth flow

The gateway's chat path uses Bearer-token API key auth
(`rust/crates/corlinman-gateway/src/middleware/auth.rs:1` —
`API_Key (Bearer) for /v1/*`). The admin path uses HTTP Basic
+ session cookie (`middleware/admin_auth.rs:1-14`).

```
First launch:
  OnboardingView captures: gateway URL, admin username, admin password
  → POST /admin/auth/login  (basic auth) → session cookie returned
  → store cookie + creds in Keychain (service: "com.corlinman.mac.admin")

  → GET /admin/tenants?for_user=<username>  → list of tenants the user can access
  → pick one (or auto-pick if singleton)
  → store selected tenant slug in UserDefaults (non-secret)

  → POST /admin/api_keys  body { tenant, scope: "chat" }  → returns { api_key }
  → store api_key in Keychain (service: "com.corlinman.mac.chat")

Subsequent launches:
  Read api_key from Keychain → use as Bearer token on every /v1/* request
  Read admin cookie from Keychain → use for any /admin/* call
  Cookie expired → silently re-login from stored creds; if creds rejected,
                   bounce to OnboardingView
```

Multi-tenant selection lives in the toolbar: a `Picker` populated
from `GET /admin/tenants?for_user=…`. Switching tenants rotates the
api_key (issuing a new one against the new tenant) and clears the
local session cache (sessions are tenant-scoped per
`routes/admin/sessions.rs:30`).

The "request a chat-scoped api_key" endpoint is **not** today wired —
the gateway issues api_keys at boot via config. **Action item for
B-side**: gateway needs a `POST /admin/api_keys` endpoint that mints
a per-(user,tenant) bearer token. Cited as a dependency, not built
inside C4.

## Streaming UX

SwiftUI's `.task { }` modifier hosts the chat-stream consumer:

```swift
struct ChatView: View {
    @Bindable var viewModel: ChatViewModel
    var body: some View {
        VStack {
            MessageList(messages: viewModel.messages)
            Composer(onSend: viewModel.send)
        }
        .task(id: viewModel.activeStreamId) {
            guard let stream = viewModel.activeStream else { return }
            for try await chunk in stream {
                viewModel.apply(chunk)        // appends token / opens approval / closes turn
            }
        }
    }
}
```

`ChatStream` is `AsyncSequence<ChatChunk>` where `ChatChunk` is the
discriminated union over `{ tokenDelta, toolCallDelta, awaitingApproval,
done, error }` — one Swift case per `ServerFrame.kind` variant
(`agent.proto:110-119`). The SSE parser in `CorlinmanCore` reads
`data: …` lines off URLSession's `bytes(for:)` async sequence,
strips `data: [DONE]` as the terminal sentinel, JSON-decodes the
remaining lines into `ChatChunk`s.

**Cancellation**: pressing the stop button cancels the SwiftUI
`.task`. URLSession sees the cancel, the SSE stream tears down,
and the gateway sees a client disconnect — which propagates to the
Python agent as `Cancel` (`agent.proto:94-97`). No explicit cancel
RPC needed; HTTP-level disconnect carries the signal.

**Tool-approval modal**: when `ChatChunk.awaitingApproval` arrives,
the view model presents a sheet (`ApprovalSheet.swift`) with the
plugin/tool name + args preview. User picks approve/deny + scope.
The choice goes back as `POST /v1/chat/completions/:turn_id/approve`
body `{ call_id, approved, scope, deny_message? }` — a separate
HTTP call, not pushed back through the SSE stream. **Action item for
B-side**: gateway needs this endpoint; today the approval round-trip
is internal to the agent gRPC stream and has no external HTTP
surface. Mock the endpoint in C4's tests; flag the gateway gap.

## Push surface

Two variants share one schema. The schema lives in a new proto
message added to `agent.proto` (or a new `push.proto`):

```protobuf
message PushNotification {
  string id = 1;                  // server-generated, dedup key
  string tenant_id = 2;
  string user_id = 3;             // canonical (post-B2 resolution)
  PushKind kind = 4;
  string title = 5;
  string body = 6;
  // Deep-link target — chat session, approval id, etc.
  string deep_link = 7;
  uint64 created_at_ms = 8;
}
enum PushKind {
  PUSH_KIND_UNSPECIFIED = 0;
  PUSH_KIND_APPROVAL_REQUIRED = 1;
  PUSH_KIND_TASK_COMPLETED = 2;
  PUSH_KIND_EVOLUTION_APPLIED = 3;
}
```

### Production: APNs

Gateway gains an `apns_channel` adapter alongside `qq` and
`telegram` — same `Channel` trait at
`rust/crates/corlinman-channels/src/channel.rs:75-93`. It reads
device tokens from a new `device_tokens` table (per-tenant,
per-user) and POSTs to APNs HTTP/2 with a JWT signed by the
operator's APNs auth key (config under `[channels.apns]`).

The Mac app registers for remote notifications on launch, captures
the device token from `application(_:didRegisterForRemoteNotifications…)`,
and sends it to the gateway via `POST /v1/devices` with the api_key.
Token rotation handled the same way (re-register on token change).

### Dev: Unix-domain socket

For iteration without an Apple Developer account, real APNs is
useless. Instead, the gateway writes the same `PushNotification`
payload as a JSON line to a Unix socket at
`<data_dir>/dev_push.sock`. The Mac app, when launched with
`CORLINMAN_DEV_PUSH_SOCKET=<path>` env var, opens that socket and
reads lines. UI is identical: `PushReceiver` exposes
`AsyncSequence<PushNotification>`, and `OnboardingView` /
`AppDelegate` consume it the same way regardless of source.

The dev socket lives behind `[channels.dev_push] enabled = true` in
`corlinman.toml` so prod deployments don't accidentally enable it.
Tests use a `tempfile`-backed socket per process.

## Persistence

`SessionStore` wraps GRDB (SQLite for Swift, mature). Schema:

```sql
CREATE TABLE sessions (
  session_key TEXT PRIMARY KEY,
  tenant_slug TEXT NOT NULL,
  display_title TEXT,                    -- first user message, truncated
  last_message_at INTEGER NOT NULL,      -- unix ms
  created_at INTEGER NOT NULL
);
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_key TEXT NOT NULL REFERENCES sessions(session_key) ON DELETE CASCADE,
  role TEXT NOT NULL,                    -- "user" | "assistant" | "tool"
  content TEXT NOT NULL,
  tool_call_id TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_messages_session ON messages(session_key, created_at);
```

Stored at `~/Library/Application Support/Corlinman/sessions.sqlite`.
Per-tenant scoping is enforced by `tenant_slug` column + a row-
filter in queries — single file is simpler than per-tenant files
for a desktop client, and operator switching tenants is rare.

**Resync on launch**: the local cache is authoritative for offline
view. On launch, `GET /admin/sessions?since=<last_local_ts>` pulls
any sessions that have moved on the server (mirrors
`routes/admin/sessions.rs:112` shape). New sessions (created on
another device) appear with their server-side `last_message_at`;
local-only sessions stay until they hit the server on next send.

## Test matrix

| Test | Layer | Asserts |
|---|---|---|
| `proto_codec_round_trips_attachment` | core | `Attachment` proto roundtrips JSON-encode/decode without loss |
| `chat_stream_parses_token_deltas` | core | SSE `data: {...}` lines → ordered `tokenDelta` chunks |
| `chat_stream_parses_tool_call_deltas` | core | OpenAI-streaming tool_calls accumulate by index → final shape matches `agent.proto:129-135` |
| `chat_stream_handles_done_sentinel` | core | `data: [DONE]` closes the AsyncSequence |
| `chat_stream_propagates_cancellation` | core | Task cancel → URLSession cancel → no further chunks emitted |
| `mock_server_full_turn` | integration | Mock SSE server → end-to-end: send → 5 token deltas → `awaitingApproval` → approve → 3 more tokens → `done` |
| `auth_store_keychain_round_trip` | core | Save/read api_key; deleted key reads as nil |
| `auth_store_first_launch_onboarding` | core | No stored cred → `requiresOnboarding == true` |
| `session_store_persists_across_relaunch` | core | Open store, append, close, reopen, query → row present |
| `session_store_resync_merges_server_changes` | core | Local cache + `GET /admin/sessions` response → merged list correct |
| `push_receiver_dev_socket_emits_payloads` | core | Write JSON line to test socket → `AsyncSequence` yields one `PushNotification` |
| `push_receiver_apns_payload_decodes` | core | Sample APNs payload → same `PushNotification` shape |
| `chat_view_snapshot_idle` | UI | Empty session → snapshot matches |
| `chat_view_snapshot_streaming_typing_indicator` | UI | Mid-stream → typing dots visible |
| `approval_sheet_snapshot` | UI | `awaitingApproval` chunk → sheet contents match (plugin, tool, args preview) |
| `tenant_picker_swaps_api_key` | integration | Switch tenant → next request uses new api_key + cleared session list |

Snapshot tests via `swift-snapshot-testing` (Pointfree); CI runs on
`macos-latest`. UI tests use a fixed-clock + fixed-randomness
viewModel so snapshots are deterministic.

## Out of scope

- **iOS / iPadOS shipping** — the SwiftPM split makes a future iOS
  target small, but C4 builds only macOS. Touch handlers, push
  entitlements, and Mac App Store distribution are deferred.
- **Mac App Store distribution** — code-signing dance, sandbox
  entitlements, network entitlement justification. C4 ships a dev
  build (notarized at most for distribution outside the store).
- **SwiftUI-Lifecycle vs AppDelegate-bridge debate** — APNs token
  registration needs `NSApplicationDelegateAdaptor`, full stop. No
  pure-SwiftUI alternative exists for that callback as of macOS 14.
  Documented in `CorlinmanApp.swift` next to the adaptor.
- **Voice input** — D4. Out.
- **MCP integration** — C1 + C2. Out.
- **Admin / agent management UIs** — web UI covers it. Mac app is a
  chat client, not a console.
- **Cross-deployment client federation** — the app talks to one
  gateway at a time. Switching gateways = re-running onboarding.

## Config / build

### Bootstrap (operator side)

```sh
# From the repo root:
cd apps/swift-mac
swift build              # SwiftPM resolves deps + runs the protoc plugin
swift test               # unit + UI tests against an in-process mock server
swift run CorlinmanApp   # opens the SwiftUI window; first run shows OnboardingView
```

### Gateway-side config additions

```toml
[channels.apns]
enabled = false                              # default off
auth_key_path = "/etc/corlinman/apns_auth.p8"
key_id = "ABC123XYZ"
team_id = "TEAM7777"
bundle_id = "com.corlinman.mac"

[channels.dev_push]
enabled = false                              # default off; turn on for Mac dev
socket_path = "/tmp/corlinman_dev_push.sock"
```

### CI

`apps/swift-mac/.github/workflows/swift.yml`:

- `runs-on: macos-latest`
- `swift test --enable-code-coverage`
- snapshot artifacts uploaded on failure

CI is a separate workflow file from the existing Rust + Python
+ Node CI; macOS runners are slower and more expensive, so it
runs only when `apps/swift-mac/**` changes (paths filter).

## Open questions

1. **Public gateway gRPC surface — defer or commit?** This doc
   chooses HTTP+SSE for chat, citing simplicity and existing-client
   parity. iOS will eventually want bidir streaming with proper
   backpressure (the Python agent already has it via
   `agent.proto:21-23` but it's not exposed). Decision deferrable
   until the Swift app proves the SSE path's pain points; revisit
   in W3 retro.
2. **APNs in dev without an Apple account.** The Unix-socket fallback
   works for the iteration loop, but a developer who joined the
   Apple Developer Program will want real APNs end-to-end. Pin a
   testing protocol: how does a dev verify the APNs path without
   shipping to TestFlight? Lean: a separate `dev_apns_simulator`
   target in `apps/swift-mac/Tests/` that POSTs a known payload to
   the gateway's dev-mode `POST /admin/dev/push`, the gateway
   echoes through the real APNs path against a sandbox device
   token. Stretch goal — stub-socket is enough for B-side.
3. **Embedded fallback LLM?** When the gateway is unreachable, should
   the Mac app expose a tiny on-device model (CoreML wrapper around
   a small Llama)? Lean: **no, ever.** The reference client's
   purpose is to demonstrate the *gateway contract*; an offline
   fallback dilutes that and ships an unmaintained second pipeline.
   If users want offline, that's a different product.
4. **Snapshot diff workflow.** Snapshot tests notoriously break on
   macOS upgrades (font metrics shift). Pin the snapshot suite to a
   specific macOS toolchain in CI and warn in the README that
   snapshots can diverge between dev machines. Lean: tolerated
   diff < 1% pixel-difference, fail otherwise.

## Implementation order — 10 iterations

Each iteration is bounded (~1d each). After iter 5 the app talks to
a mock server end-to-end; iter 6-10 move to the live gateway. iter
4 deliberately ships the dev-socket push first — APNs needs Apple
Developer enrolment which gates iteration speed.

1. **Skeleton** — `apps/swift-mac/` directory; `Package.swift` with
   three targets (`CorlinmanCore`, `CorlinmanUI`, `CorlinmanApp`);
   `.gitignore` for `.build/` + `.swiftpm/`; placeholder
   `CorlinmanApp.swift` that opens an empty window. README quickstart.
   No proto integration yet. Tests: `app_launches_without_crash`.
2. **Proto codegen** — wire the `swift-protobuf` SwiftPM plugin;
   build resolves `../../proto/corlinman/v1/*.proto`; `import
   CorlinmanProto` works inside `CorlinmanCore`. Tests:
   `proto_codec_round_trips_attachment` + one for `ServerFrame`.
3. **`GatewayClient` skeleton + mock server** — non-streaming
   `/v1/chat/completions` POST with Bearer; mock server in
   `Tests/Helpers/MockGateway.swift` (NIO-based, 50-line).
   Tests: `mock_server_non_streaming_round_trip`.
4. **`ChatStream` SSE parser** — URLSession `bytes(for:)` →
   `AsyncSequence<ChatChunk>`. Cancel propagation. Tests:
   `chat_stream_parses_token_deltas`, `_tool_call_deltas`,
   `_handles_done_sentinel`, `_propagates_cancellation`.
5. **`AuthStore` + Keychain** — wrap `Security.framework` for the
   two services (admin, chat); save/read/delete. Onboarding
   detection. Tests: `auth_store_keychain_round_trip`,
   `_first_launch_onboarding`. (Use `XCTSkipUnless` for keychain
   access on CI without a signing identity — fall back to in-memory.)
6. **`SessionStore` (GRDB)** — schema + CRUD; resync helper
   `mergeServerSessions(_:)`. Tests: `_persists_across_relaunch`,
   `_resync_merges_server_changes`.
7. **`PushReceiver` dev-socket variant + gateway-side stub** —
   gateway adds `[channels.dev_push]` config + a tiny background
   task that writes JSON lines to the configured socket on a
   `dev_push_test` admin trigger. Swift `PushReceiver` opens the
   socket and yields `PushNotification`s. Tests: `_dev_socket_emits_payloads`.
8. **SwiftUI `ChatView` + `Composer` + `MessageList`** — wired to
   the streaming `ChatViewModel`. Snapshot tests
   `chat_view_snapshot_idle`, `_streaming_typing_indicator`. UI is
   intentionally plain; a future iter (or the design skill) can
   pretty it up.
9. **`ApprovalSheet` + approve POST endpoint integration** —
   surfacing `awaitingApproval` chunks; calling the
   to-be-built `POST /v1/chat/completions/:turn_id/approve`. Mock
   the endpoint in tests. Snapshot test `approval_sheet_snapshot`.
   File a separate gateway issue for the real endpoint.
10. **End-to-end against dev gateway** — operator runs `cargo run -p
    corlinman-gateway` with `[channels.dev_push] enabled = true`,
    then `swift run CorlinmanApp`. Onboard → chat → approve a tool
    call → trigger a dev-push → notification banner appears.
    Documented in README as the smoke-test recipe. CI cannot run
    this (no real gateway), so it's a manual gate. The 10-iter exit
    criterion: this script works on a clean macbook checkout in
    < 10 minutes.

## Demo contract — what iOS / Android teams inherit

The deliverable for downstream client teams is **not** the SwiftUI
code. It's the contract pinned by C4. Concretely:

1. **`apps/swift-mac/README.md` — "Building a corlinman client"** —
   step-by-step in Swift terms but transferrable: `(a)` how SSE is
   framed (header, `data:` lines, `[DONE]`); `(b)` how OpenAI-style
   tool-call deltas accumulate; `(c)` Bearer token format; `(d)`
   tenant selection mechanics; `(e)` push payload schema; `(f)` the
   dev-socket fallback so iOS devs can iterate without TestFlight.
2. **Generated proto bindings** — the same `swift-protobuf`-
   generated `.pb.swift` files double as a reference for what an
   Android team's `protoc-gen-kotlin` invocation should produce.
   The gateway-side proto files (`proto/corlinman/v1/*.proto`) are
   the source of truth; this is just one consumer.
3. **`docs/clients/contract.md`** — language-neutral spec extracted
   from the README during iter 10. Schema, lifecycle, error codes,
   keep-alive cadence, push payloads, auth flows. Lives at the same
   level as roadmap docs so iOS / Android teams find it without
   reading Swift.
4. **A working app that another engineer can `git clone && swift
   run`** — the most under-rated artifact. When the iOS team starts,
   they don't argue with a spec; they run the Swift app, watch
   packets in Charles Proxy, and replicate. Specs lie; running code
   doesn't.
