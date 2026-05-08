// Phase 4 W3 C4 iter 1 — `@main` entry point for the macOS reference client.
//
// At iter 1 the app opens an empty SwiftUI window with a placeholder view.
// Iter 8 wires `OnboardingView` / `ChatView` selection based on
// `AuthStore.requiresOnboarding`; iter 4 attaches `NSApplicationDelegateAdaptor`
// for APNs registration (see `docs/design/phase4-w3-c4-design.md:380-383`
// for why an AppDelegate adaptor is required even in pure SwiftUI lifecycle).

import SwiftUI

import CorlinmanCore
import CorlinmanUI

@main
struct CorlinmanApp: App {
    var body: some Scene {
        WindowGroup {
            PlaceholderRootView()
                .onAppear {
                    // Surface a build banner the test fixture and ops
                    // smoke-checks can rely on. Replaced with structured
                    // logging once `CorlinmanCore` grows a logger.
                    print(CorlinmanCoreInfo.banner)
                }
        }
    }
}
