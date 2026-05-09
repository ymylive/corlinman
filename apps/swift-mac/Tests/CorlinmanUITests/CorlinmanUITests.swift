// Phase 4 W3 C4 iter 1 — smoke tests for the `CorlinmanUI` skeleton.
//
// Iter 1 only checks the placeholder view instantiates without crashing.
// Snapshot tests land in iter 8 once `ChatView` / `ApprovalSheet` exist.

import XCTest
import SwiftUI

@testable import CorlinmanUI

final class CorlinmanUITests: XCTestCase {
    /// Iter 1 contract: `PlaceholderRootView` constructs without trapping.
    /// A stronger snapshot harness arrives at iter 8.
    func test_placeholderRootView_constructs() {
        let view = PlaceholderRootView()
        // Touch `body` on the main actor so SwiftUI's body builder runs at
        // least once. We don't assert on the rendered output; the goal is
        // to flush any compile-time / init-time crashers.
        _ = view.body
    }
}
