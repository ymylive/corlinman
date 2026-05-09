// Phase 4 W3 C4 — umbrella file for `CorlinmanUI`.
//
// Iter 6 split the concrete views (`ChatView`, `MessageList`,
// `Composer`) into their own file and added `ChatViewModel`. The
// placeholder root view stays here as a minimal fallback the App
// can render before auth/store wiring is complete (today the App
// uses it; iter 7+ swaps in `OnboardingView` → `ChatView`).

import SwiftUI

/// First-launch placeholder view. Iter 7+ swaps this for an
/// `OnboardingView` based on `AuthStore`'s onboarding detection. Kept
/// as a struct (not a function) so the future swap is a rename
/// rather than a re-architecture. The body advertises the current
/// iter so a stale binary is obvious from a glance.
public struct PlaceholderRootView: View {
    public init() {}

    public var body: some View {
        VStack(spacing: 12) {
            Text("Corlinman macOS reference client")
                .font(.title2)
            Text("Phase 4 W3 C4 — iter 6 (chat view scaffolded)")
                .font(.callout)
                .foregroundStyle(.secondary)
            Text("Streaming + persistence wired in CorlinmanCore/CorlinmanUI; auth + onboarding land in iter 7+.")
                .font(.footnote)
                .foregroundStyle(.tertiary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 32)
        }
        .padding(48)
        .frame(minWidth: 480, minHeight: 320)
    }
}
