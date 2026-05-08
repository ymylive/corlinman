# Corlinman macOS reference client

Phase 4 W3 C4 — a SwiftUI macOS app that drives the gateway end-to-end so
iOS / Android teams can copy a working pattern instead of reverse-
engineering one from `routes/chat.rs`. See
[`docs/design/phase4-w3-c4-design.md`](../../docs/design/phase4-w3-c4-design.md)
for the full design, scope, and 10-iteration plan.

This is **not** a shipping product. Its job is to pin the wire contract.

## Status — iter 1 (skeleton)

- `Package.swift` declares three targets: `CorlinmanCore`,
  `CorlinmanUI`, `CorlinmanApp`.
- `swift package describe` succeeds with no external dependencies.
- `swift build` produces an empty SwiftUI window via `CorlinmanApp`.
- `swift test` runs two trivial smoke tests (one per target).

Future iterations:

| Iter | Surface |
|---|---|
| 2 | swift-protobuf integration + codec round-trip tests |
| 3 | `GatewayClient` skeleton + per-turn approve route on gateway |
| 4 | `ChatStream` SSE parser |
| 5 | `AuthStore` + Keychain wrapper |
| 6 | `SessionStore` (GRDB) |
| 7 | `PushReceiver` dev-socket variant |
| 8 | `ChatView` + composer |
| 9 | `ApprovalSheet` |
| 10 | End-to-end against a dev gateway |

## Quickstart

```sh
cd apps/swift-mac
swift build
swift test
swift run CorlinmanApp
```

`swift build` requires Swift 5.9+ and macOS 13+. CI installs nothing
beyond the Swift toolchain.

## Why a separate top-level `apps/` directory

- Outside the Cargo workspace (`rust/`).
- Outside the npm workspace (`ui/`).
- Outside the Python monorepo (`python/`).
- Sibling, not nested, so a future iOS app target can drop alongside
  it without re-rooting the SwiftPM manifest.
