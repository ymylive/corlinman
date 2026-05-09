// Phase 4 W3 C4 — smoke tests for the `CorlinmanCore` build metadata.
//
// Asserting on the banner string costs nothing and catches the
// "forgot to bump the version when shipping a new surface" mistake on
// every CI run. The actual chat / persistence / push tests live in
// their own files (`ChatStreamTests.swift`, `SessionStoreTests.swift`,
// `PushReceiverTests.swift` as they land).

import XCTest

@testable import CorlinmanCore

final class CorlinmanCoreTests: XCTestCase {
    /// Iter 9 contract: banner advertises chat-ui+auth+push+ci and
    /// the version is at `0.6.1` (snapshot scaffolding only — patch
    /// bump). Bumped each time `CorlinmanCore` gains a non-additive
    /// public surface, or once per ship-able iteration.
    func test_buildInfo_reportsCurrentIter() {
        XCTAssertTrue(
            CorlinmanCoreInfo.banner.contains("chat-ui+auth+push+ci"),
            "build banner must advertise chat-ui+auth+push+ci; got \(CorlinmanCoreInfo.banner)"
        )
        XCTAssertEqual(CorlinmanCoreInfo.version, "0.6.1")
    }
}
