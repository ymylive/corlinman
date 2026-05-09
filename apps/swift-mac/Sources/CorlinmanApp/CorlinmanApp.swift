// Phase 4 W3 C4 iter 1+ — `@main` entry point for the macOS reference client.
//
// Iter 7 wires `OnboardingView` for the first-launch path and falls
// through to the placeholder once auth is staged. The placeholder
// stays in the picture until iter 10 swaps it for the streaming
// `ChatView` bound to a real `ChatViewModel`. Keeping the swap
// minimal here means the `OnboardingViewModel`-rendered flow is the
// only state machine the app ships at this iter.
//
// Iter 8 will attach `NSApplicationDelegateAdaptor` for APNs
// registration (see `docs/design/phase4-w3-c4-design.md:380-383`
// for why an AppDelegate adaptor is required even in pure SwiftUI
// lifecycle).

import SwiftUI

import CorlinmanCore
import CorlinmanUI

@main
struct CorlinmanApp: App {
    @StateObject private var session = AppSession()

    var body: some Scene {
        WindowGroup {
            RootView(session: session)
                .onAppear {
                    print(CorlinmanCoreInfo.banner)
                }
        }
    }
}

/// Holds the live AuthStore + tracks whether we're past onboarding.
/// `@MainActor` because `OnboardingViewModel` and the swap logic are
/// SwiftUI-bound.
@MainActor
final class AppSession: ObservableObject {
    @Published var requiresOnboarding: Bool

    let authStore: AuthStore

    init() {
        #if canImport(Security)
        let store = AuthStore.defaultStore()
        #else
        let store = AuthStore(keychain: InMemoryKeychain())
        #endif
        self.authStore = store
        self.requiresOnboarding = store.requiresOnboarding
    }

    /// Called by the onboarding view model when minting succeeds.
    /// Bumps `requiresOnboarding` to false so the root view swaps.
    func didCompleteOnboarding(_: StoredCredentials) {
        self.requiresOnboarding = false
    }
}

/// Top-level routing: onboarding vs post-auth placeholder. Each
/// route owns its own view model so re-entering onboarding (e.g.
/// after a "log out" gesture in a future iter) starts from a clean
/// state.
struct RootView: View {
    @ObservedObject var session: AppSession

    var body: some View {
        if session.requiresOnboarding {
            OnboardingView(viewModel: OnboardingViewModel(
                authStore: session.authStore,
                onComplete: { creds in
                    session.didCompleteOnboarding(creds)
                }
            ))
        } else {
            PlaceholderRootView()
        }
    }
}
