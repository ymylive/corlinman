// Phase 4 W3 C4 iter 1+ — `@main` entry point for the macOS reference client.
//
// Iter 7 wires `OnboardingView` for the first-launch path and falls
// through to the placeholder once auth is staged. Iter 8 attaches an
// `NSApplicationDelegateAdaptor` so APNs can hand back device tokens
// (`docs/design/phase4-w3-c4-design.md:380-383` documents why a pure
// SwiftUI lifecycle alone can't capture this callback). The placeholder
// stays in the picture until iter 10 swaps it for the streaming
// `ChatView` bound to a real `ChatViewModel`.

import SwiftUI
#if canImport(AppKit)
import AppKit
#endif
#if canImport(UserNotifications)
import UserNotifications
#endif

import CorlinmanCore
import CorlinmanUI

@main
struct CorlinmanApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var session = AppSession()

    var body: some Scene {
        WindowGroup {
            RootView(session: session)
                .onAppear {
                    print(CorlinmanCoreInfo.banner)
                    appDelegate.session = session
                }
        }
    }
}

/// AppDelegate hook for APNs registration. SwiftUI's pure-lifecycle
/// API can't capture `application(_:didRegisterForRemoteNotificationsWithDeviceToken:)`
/// — the design doc spells out why at line 380-383. Keeping the
/// delegate small (only the APNs callbacks live here) limits the
/// blast radius of mixing AppKit lifecycle into the SwiftUI app.
final class AppDelegate: NSObject {
    /// Reference to the SwiftUI session, populated post-launch by
    /// `RootView.onAppear`. We need a hand-off because the delegate
    /// itself is constructed before the SwiftUI tree exists.
    @MainActor weak var session: AppSession?
}

#if canImport(AppKit)
extension AppDelegate: NSApplicationDelegate, UNUserNotificationCenterDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Ask for permission + register for remote notifications.
        // Under the dev-socket fallback we still want this so that
        // operators who provisioned APNs see real tokens land in
        // `AuthStore`; under no-entitlement runs the delegate
        // callbacks simply don't fire and we fall through to the
        // dev socket.
        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
            if granted {
                DispatchQueue.main.async {
                    NSApplication.shared.registerForRemoteNotifications()
                }
            }
        }
    }

    /// APNs handed back a device token. Stash it in the active
    /// session's APNs adapter so the `AsyncSequence<PushNotification>`
    /// surface stays unified.
    func application(
        _ application: NSApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        Task { @MainActor in
            session?.didReceiveAPNsToken(deviceToken)
        }
    }

    /// APNs registration failed — typically because the binary is
    /// unsigned or the device isn't reachable. Logged for visibility;
    /// the dev-socket fallback keeps the push surface alive.
    func application(
        _ application: NSApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        print("[apns] register failed: \(error.localizedDescription)")
    }

    /// Foreground notification arrival.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        let userInfo = notification.request.content.userInfo
        Task { @MainActor in
            session?.didReceiveAPNsPayload(userInfo)
        }
        completionHandler([.banner, .sound, .badge])
    }
}
#endif

/// Holds the live AuthStore + APNs adapter + tracks whether we're
/// past onboarding. `@MainActor` because `OnboardingViewModel` and
/// the swap logic are SwiftUI-bound.
@MainActor
final class AppSession: ObservableObject {
    @Published var requiresOnboarding: Bool

    let authStore: AuthStore
    let apnsAdapter: APNsTokenAdapter
    let pushReceiver: PushReceiver

    init() {
        #if canImport(Security)
        let store = AuthStore.defaultStore()
        #else
        let store = AuthStore(keychain: InMemoryKeychain())
        #endif
        self.authStore = store
        self.requiresOnboarding = store.requiresOnboarding
        let adapter = APNsTokenAdapter()
        self.apnsAdapter = adapter
        // Pick the push transport based on env var: setting
        // `CORLINMAN_DEV_PUSH_SOCKET=/path/to/sock` opts into the
        // dev socket, otherwise we wire APNs. Mirrors the design
        // doc §"Dev: Unix-domain socket" line 302.
        if let socket = ProcessInfo.processInfo.environment["CORLINMAN_DEV_PUSH_SOCKET"], !socket.isEmpty {
            self.pushReceiver = PushReceiver(transport: .devSocket(path: socket))
        } else {
            self.pushReceiver = PushReceiver(transport: .apns(adapter: adapter))
        }
    }

    /// Called by the onboarding view model when minting succeeds.
    /// Bumps `requiresOnboarding` to false so the root view swaps.
    func didCompleteOnboarding(_: StoredCredentials) {
        self.requiresOnboarding = false
    }

    /// Forwarded by the AppDelegate when APNs hands back a device
    /// token. The token gets POSTed to the gateway by a future iter
    /// (10) — for now we just hold it on the adapter.
    func didReceiveAPNsToken(_ token: Data) {
        apnsAdapter.didRegisterDeviceToken(token)
    }

    /// Forwarded by the AppDelegate when an APNs payload arrives.
    /// Decodes through the adapter so the unified push stream sees
    /// the same `PushNotification` shape as the dev socket.
    func didReceiveAPNsPayload(_ userInfo: [AnyHashable: Any]) {
        apnsAdapter.deliverApnsUserInfo(userInfo)
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
