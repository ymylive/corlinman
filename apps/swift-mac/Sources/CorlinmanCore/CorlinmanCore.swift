// Phase 4 W3 C4 iter 1 — placeholder umbrella file for `CorlinmanCore`.
//
// Iter 3+ replaces this with `GatewayClient.swift`, `ChatStream.swift`,
// `AuthStore.swift`, `SessionStore.swift`, `PushReceiver.swift`, and
// `Models.swift` per the file plan in
// `docs/design/phase4-w3-c4-design.md:97-105`. Today the module just
// publishes a build-info constant so the test target has something
// non-trivial to import without forcing a follow-up rebuild on every
// future surface addition.

import Foundation

/// Static metadata about the `CorlinmanCore` build. Updated as the module
/// gains concrete surfaces; the test target asserts on the version string
/// so a stray downgrade fails CI loudly.
public enum CorlinmanCoreInfo {
    /// Human-readable build banner. Only used in logs and tests today.
    public static let banner = "CorlinmanCore (Phase 4 W3 C4 iter 1 skeleton)"

    /// Bumped every time the public surface changes in a non-additive way.
    /// Iter 1 ships at `0.1.0`; iter 2's proto integration will move it to
    /// `0.2.0`.
    public static let version = "0.1.0"
}
