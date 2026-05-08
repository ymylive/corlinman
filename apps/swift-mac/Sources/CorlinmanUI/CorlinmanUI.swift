// Phase 4 W3 C4 iter 1 — placeholder umbrella file for `CorlinmanUI`.
//
// Iter 8+ replaces this with `ChatView.swift`, `SessionListView.swift`,
// `ApprovalSheet.swift`, `OnboardingView.swift`, and `Theme.swift` per
// `docs/design/phase4-w3-c4-design.md:106-111`. Today the module just
// publishes a placeholder root SwiftUI view so the executable target
// has something to render.

import SwiftUI

/// First-launch placeholder view. Iter 8 swaps this for
/// `OnboardingView` / `ChatView` based on `AuthStore`'s onboarding
/// detection. Kept as a struct (not a function) so the future swap is a
/// rename rather than a re-architecture.
public struct PlaceholderRootView: View {
    public init() {}

    public var body: some View {
        VStack(spacing: 12) {
            Text("Corlinman macOS reference client")
                .font(.title2)
            Text("Phase 4 W3 C4 — iter 1 skeleton")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .padding(48)
        .frame(minWidth: 480, minHeight: 320)
    }
}
