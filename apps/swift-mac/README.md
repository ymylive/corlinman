# Corlinman macOS reference client

Phase 4 W3 C4 — a SwiftUI macOS app that drives the gateway end-to-end so
iOS / Android teams can copy a working pattern instead of reverse-
engineering one from `routes/chat.rs`. See
[`docs/design/phase4-w3-c4-design.md`](../../docs/design/phase4-w3-c4-design.md)
for the full design, scope, and 10-iteration plan.

This is **not** a shipping product. Its job is to pin the wire contract.

## Status — iter 10 (close-out)

C4 ships ten iterations:

| Iter | Surface | Lands |
|---|---|---|
| 1  | SwiftPM skeleton (3 targets, `swift build` green) | ✅ |
| 2  | Codable models for the SSE chunk shapes (proto/codegen still in scope; today the JSON shapes mirror `agent.proto`) | ✅ |
| 3  | `GatewayClient` admin-REST shim + `POST /v1/chat/completions/:turn_id/approve` route on the gateway | ✅ |
| 4  | `ChatStream` SSE parser + `Models.swift` chunk decoder | ✅ |
| 5  | `SessionStore` (SQLite) | ✅ |
| 6  | `ChatView` + `ChatViewModel` | ✅ |
| 7  | `AuthStore` + Keychain + `OnboardingView` | ✅ |
| 8  | `PushReceiver` (APNs adapter + dev-socket fallback) | ✅ |
| 9  | Snapshot tests + macOS CI workflow | ✅ |
| 10 | `ApprovalClient` + `ApprovalSheet` + `LiveChatStreamSource` + `MainShellView` + E2E acceptance tests + this README + `docs/auth-flow.md` + `docs/wire-protocol.md` | ✅ |

Demo contract artefacts shipped at iter 10:

- This README — operator quickstart + key-files map for iOS/Android
  porters.
- [`docs/auth-flow.md`](docs/auth-flow.md) — single-page protocol
  writeup for the onboarding + Bearer + tenant-switch dance.
- [`docs/wire-protocol.md`](docs/wire-protocol.md) — language-neutral
  spec of the SSE shape, custom event types, and approval round-trip.

## Quickstart

### Pre-requisites

- macOS 13 (Ventura) or newer.
- Swift 5.9 + Xcode command-line tools (or full Xcode 15+).
  `xcode-select --install` if you don't have them.
- A running Corlinman gateway. Either:
  - **Local dev**: `cargo run -p corlinman-gateway` from the repo root
    with a `corlinman.toml` that has `[channels.dev_push] enabled = true`
    if you want to exercise the push surface (today's gateway writer
    is wire-stubbed; see "Known gaps" below).
  - **Remote**: any deployment of the gateway you have admin
    credentials for.

### Build + run

```sh
cd apps/swift-mac
swift build              # SwiftPM resolves zero deps; clean build
swift test               # unit + UI tests (macOS CI runs the snapshot
                         #   block; locally CommandLineTools won't
                         #   have XCTest — use Xcode if you want the
                         #   tests to actually execute)
swift run CorlinmanApp   # opens the SwiftUI window
```

`swift run` opens directly into `OnboardingView` on first launch.
After onboarding the operator lands on `MainShellView` (live `ChatView`
bound to `ChatViewModel` against the operator's gateway).

### First-launch walkthrough

1. **Onboarding** — fill in:
   - **Gateway URL** — `https://your-gateway.example.com` (no trailing
     slash needed; `URL(string:relativeTo:)` handles both forms).
   - **Admin username + password** — used for `POST /admin/auth/login`
     to mint the chat-scoped api_key. Stored in macOS Keychain under
     `com.corlinman.mac.admin`.
   - The view model calls `GET /admin/tenants?for_user=…`; if the
     operator has access to multiple tenants, a picker shows up.
     Single-tenant operators auto-confirm.
   - Final step: `POST /admin/api_keys { scope: "chat" }` mints a
     bearer token. Stored under `com.corlinman.mac.chat`.
2. **First chat turn** — type a message, hit Enter. The app:
   - Builds an OpenAI-style `chat.completions` POST with
     `stream: true` and a corlinman-extension `session_key` field.
   - `URLSession.bytes(for:)` opens the SSE stream; `ChatStream` parses
     `data: …` lines into `ChatChunk` enum cases.
   - Token deltas append to the assistant message in `ChatView`.
   - `data: [DONE]` closes the stream; the assistant message and
     session row land in `~/Library/Application Support/Corlinman/sessions.sqlite`.
3. **Approval prompt** (when the agent asks for tool approval) —
   `event: awaiting_approval` SSE frame surfaces a sheet with the
   plugin/tool/args preview. Approve/Deny + scope; the choice POSTs
   to `/v1/chat/completions/:turn_id/approve` with the chat-scoped
   bearer.
4. **Restart** — quit and relaunch the app. The session list (today's
   single resumed session via `loadFromCache`) shows up before any
   network round-trip.

### Smoke test

```sh
# 1. Run the gateway in dev mode against a sample tenant.
cargo run -p corlinman-gateway -- --config corlinman.local.toml

# 2. Run the Mac app pointed at it.
cd apps/swift-mac && swift run CorlinmanApp

# 3. Onboard against http://localhost:8080 with the admin credentials
#    in your config.

# 4. Ask the agent to do something that triggers a tool-approval. The
#    sheet pops; click "Approve" once. The stream resumes and the
#    final tokens render.

# 5. Quit (Cmd-Q). Relaunch. The previous turn is in the message list.
```

## Layout — what each file is for

```
apps/swift-mac/
├── Package.swift               # 3 SwiftPM targets, no external deps
├── README.md                   # this file
├── docs/
│   ├── auth-flow.md            # onboarding + Bearer + tenant switch
│   └── wire-protocol.md        # SSE framing + chunk schema
├── Sources/
│   ├── CorlinmanCore/
│   │   ├── ApprovalClient.swift          # iter 10 — POST .../approve
│   │   ├── AuthStore.swift               # iter 7  — Keychain wrapper
│   │   ├── ChatStream.swift              # iter 4  — SSE → AsyncSequence
│   │   ├── CorlinmanCore.swift           # banner + version
│   │   ├── GatewayClient.swift           # iter 7  — admin-REST shim
│   │   ├── LiveChatStreamSource.swift    # iter 10 — production binding
│   │   ├── Models.swift                  # iter 4  — ChatChunk + decoder
│   │   ├── PushReceiver.swift            # iter 8  — APNs + dev socket
│   │   └── SessionStore.swift            # iter 5  — SQLite persistence
│   ├── CorlinmanUI/
│   │   ├── ApprovalSheet.swift           # iter 10 — awaiting-approval UI
│   │   ├── ChatView.swift                # iter 6  — chat + composer
│   │   ├── ChatViewModel.swift           # iter 6  — view model glue
│   │   ├── CorlinmanUI.swift             # placeholder + module umbrella
│   │   ├── MainShellView.swift           # iter 10 — post-onboarding root
│   │   ├── OnboardingView.swift          # iter 7  — first-launch UI
│   │   └── OnboardingViewModel.swift     # iter 7  — onboarding state
│   └── CorlinmanApp/
│       └── CorlinmanApp.swift            # @main, AppDelegate, RootView
└── Tests/
    ├── CorlinmanCoreTests/
    │   ├── ApprovalClientTests.swift     # iter 10 — wire shape
    │   ├── AuthStoreTests.swift          # iter 7
    │   ├── ChatStreamTests.swift         # iter 4
    │   ├── CorlinmanCoreTests.swift      # version banner ratchet
    │   ├── PushReceiverTests.swift       # iter 8
    │   └── SessionStoreTests.swift       # iter 5
    └── CorlinmanUITests/
        ├── ChatViewModelTests.swift      # iter 6
        ├── ChatViewSnapshotTests.swift   # iter 9 — gated on dep
        ├── CorlinmanUITests.swift        # placeholder
        ├── EndToEndAcceptanceTests.swift # iter 10 — full contract
        └── OnboardingViewModelTests.swift # iter 7
```

### Key files for iOS / Android porters

- **`Models.swift`** — JSON shapes for the SSE chunk envelope, with
  one Swift case per `ServerFrame.kind` variant from
  `proto/corlinman/v1/agent.proto:110-119`. Port these to your
  language; the gateway emits the same wire shapes.
- **`ChatStream.swift`** — 60-line SSE state machine. The framing is
  vanilla SSE (data: lines, blank-line dispatch, `[DONE]` terminator)
  plus one corlinman extension: `event: awaiting_approval` for
  the tool-approval interleave.
- **`ApprovalClient.swift`** — POST body schema for the approval
  round-trip. The body is forward-compatible with future scope-tracking;
  today the gateway treats `session` / `always` as `once` (see
  `chat_approve.rs:50-54`).
- **`AuthStore.swift`** — credential layout. iOS uses the same
  Keychain; Android porters substitute Keystore.
- **`docs/auth-flow.md`** + **`docs/wire-protocol.md`** — single-page
  protocol writeups for porters who haven't read the design doc.

## Known gaps (deferred to Phase 5)

These are flagged in the iter-10 commit message but not closed inside
C4 — they are outside the C4 task budget and either need cross-team
coordination or a separate spike to ship cleanly:

1. **Gateway `dev_push` writer** — the design doc §"Push surface" /
   §"Dev: Unix-domain socket" calls for a `[channels.dev_push]`
   socket the gateway writes JSON-line `PushNotification`s into.
   The Swift reader (`PushReceiver.devSocket`) is wired and tested
   (iter 8); the gateway-side writer is not. Today the dev-push smoke
   test is exercised by `printf '{...}' >> /tmp/dev_push.sock`
   (see iter-8 PushReceiverTests). Tracked separately; the receiver
   surface stays stable.
2. **Snapshot-testing dep in committed `Package.swift`** — iter 9 ships
   a CI-job-time patcher (`.github/workflows/swift-mac.yml:71-98`)
   that injects `swift-snapshot-testing` only on the macOS runner.
   This was a deliberate trade-off so offline contributors don't see
   a hard build break; it stays in place.
3. **`POST /v1/devices` device-token registration** — APNs end-to-end
   needs a way for the Swift client to send its device token to the
   gateway. Today `APNsTokenAdapter.hexDeviceToken` exposes the token
   on the Mac side; the gateway endpoint isn't wired. Phase 5 lands
   this alongside the APNs P8 / JWT signer.

## Why a separate top-level `apps/` directory

- Outside the Cargo workspace (`rust/`).
- Outside the npm workspace (`ui/`).
- Outside the Python monorepo (`python/`).
- Sibling, not nested, so a future iOS app target can drop alongside
  it without re-rooting the SwiftPM manifest.
