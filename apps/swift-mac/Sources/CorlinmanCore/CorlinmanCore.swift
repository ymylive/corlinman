// Phase 4 W3 C4 iter 1+ — umbrella metadata for `CorlinmanCore`.
//
// Iter 4 added `Models.swift` + `ChatStream.swift` (SSE → AsyncSequence).
// Iter 5 lands `SessionStore.swift` (GRDB persistence). Iter 6 lights
// up `CorlinmanUI` views bound to streaming + persistence. The banner
// here is the only place that sees iter-level changes — concrete
// surfaces live in their own files now.

import Foundation

/// Static metadata about the `CorlinmanCore` build. Updated as the module
/// gains concrete surfaces; the test target asserts on the version string
/// so a stray downgrade fails CI loudly.
public enum CorlinmanCoreInfo {
    /// Human-readable build banner. Only used in logs and tests today.
    public static let banner = "CorlinmanCore (Phase 4 W3 C4 iter 5 — sessions)"

    /// Bumped every time the public surface changes in a non-additive way.
    /// Iter 1 → 0.1.0 (skeleton); iter 4 → 0.2.0 (chat stream + models);
    /// iter 5 → 0.3.0 (SessionStore); future iters bump for `AuthStore`,
    /// `PushReceiver`, etc.
    public static let version = "0.3.0"
}
