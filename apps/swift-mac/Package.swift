// swift-tools-version:5.9
//
// Phase 4 W3 C4 iter 1 — SwiftPM skeleton for the reference macOS client.
//
// Three targets, three jobs:
//   - CorlinmanCore  — gateway client, auth, persistence, push (zero UI deps).
//   - CorlinmanUI    — SwiftUI views + view-models that consume Core via
//                       protocols (zero networking).
//   - CorlinmanApp   — `@main` entry point + dependency wiring; glues the
//                       other two into a runnable AppKit / SwiftUI app.
//
// The split is **load-bearing for iOS portability**: dropping a future iOS
// app target reuses Core + UI verbatim and only re-implements `App`. See
// `docs/design/phase4-w3-c4-design.md:118-124` for the rationale.
//
// External dependencies are intentionally absent at iter 1. Iter 2 introduces
// `swift-protobuf` for the proto-derived JSON models; iter 6 adds GRDB for
// `SessionStore`. Keeping the manifest dependency-free at iter 1 makes
// `swift package describe` succeed even on machines without network access.

import PackageDescription

let package = Package(
    name: "CorlinmanMac",
    platforms: [
        // macOS 13 is the floor — `URLSession.bytes(for:)`, `AsyncSequence`
        // helpers, and SwiftUI `.task(id:)` all assume macOS 13+.
        .macOS(.v13),
    ],
    products: [
        .library(name: "CorlinmanCore", targets: ["CorlinmanCore"]),
        .library(name: "CorlinmanUI", targets: ["CorlinmanUI"]),
        .executable(name: "CorlinmanApp", targets: ["CorlinmanApp"]),
    ],
    dependencies: [
        // Iter 1: empty. Iter 2 will add swift-protobuf here.
    ],
    targets: [
        .target(
            name: "CorlinmanCore",
            dependencies: []
        ),
        .target(
            name: "CorlinmanUI",
            dependencies: ["CorlinmanCore"]
        ),
        .executableTarget(
            name: "CorlinmanApp",
            dependencies: ["CorlinmanCore", "CorlinmanUI"]
        ),
        .testTarget(
            name: "CorlinmanCoreTests",
            dependencies: ["CorlinmanCore"]
        ),
        .testTarget(
            name: "CorlinmanUITests",
            dependencies: ["CorlinmanUI"]
        ),
    ]
)
