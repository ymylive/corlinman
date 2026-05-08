// Phase 4 W3 C4 iter 1 — smoke tests for the `CorlinmanCore` skeleton.
//
// Just enough to prove the module imports, the package resolves, and
// `swift test` has a non-empty target. Iter 2+ adds the meaningful tests
// listed in `docs/design/phase4-w3-c4-design.md:347-371`.

import XCTest

@testable import CorlinmanCore

final class CorlinmanCoreTests: XCTestCase {
    /// Iter 1 contract: the build banner advertises iter 1 and version `0.1.0`.
    /// Future iterations bump both — a downgrade or stale string fails here
    /// before it can ship.
    func test_buildInfo_reportsIter1() {
        XCTAssertTrue(
            CorlinmanCoreInfo.banner.contains("iter 1"),
            "build banner must advertise iter 1; got \(CorlinmanCoreInfo.banner)"
        )
        XCTAssertEqual(CorlinmanCoreInfo.version, "0.1.0")
    }
}
