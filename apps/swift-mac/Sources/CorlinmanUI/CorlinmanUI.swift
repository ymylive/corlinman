// Phase 4 W3 C4 — umbrella file for `CorlinmanUI`.
//
// Iter 6 split the concrete views (`ChatView`, `MessageList`,
// `Composer`) into their own file and added `ChatViewModel`. Iter 7
// added `OnboardingView` + `OnboardingViewModel` for first-launch
// credential capture. The placeholder root view below stays as a
// minimal post-onboarding stub the App swaps in after the operator
// finishes minting an api_key — iter 10 replaces it with a real
// streaming `ChatView` wired to the live gateway.

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
            Text("Phase 4 W3 C4 — iter 7 (post-onboarding placeholder)")
                .font(.callout)
                .foregroundStyle(.secondary)
            Text("Onboarding minted an api_key. Iter 10 swaps this for the live ChatView.")
                .font(.footnote)
                .foregroundStyle(.tertiary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 32)
        }
        .padding(48)
        .frame(minWidth: 480, minHeight: 320)
    }
}
