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
    /// Iter 10 contract: banner advertises the close-out surface
    /// (e2e-acceptance + approval + demo-contract) and the version
    /// minor-bumps to `0.7.0`. Bumped each time `CorlinmanCore`
    /// gains a non-additive public surface, or once per ship-able
    /// iteration.
    func test_buildInfo_reportsCurrentIter() {
        XCTAssertTrue(
            CorlinmanCoreInfo.banner.contains("e2e-acceptance+approval+demo-contract"),
            "build banner must advertise iter 10 surfaces; got \(CorlinmanCoreInfo.banner)"
        )
        XCTAssertEqual(CorlinmanCoreInfo.version, "0.7.0")
    }
}
