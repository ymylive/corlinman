// Phase 4 W3 C4 iter 10 — `MainShellView`: the post-onboarding root
// that finally wires `ChatView` to the live gateway.
//
// Iter 7 stood up `PlaceholderRootView` as a sentinel between
// onboarding and the real app surface; iter 10 replaces it with a
// shell that constructs `ChatViewModel` against the operator's
// stored credentials. The shell is the smallest amount of glue that
// satisfies the design doc's iter-10 acceptance gate
// (`docs/design/phase4-w3-c4-design.md:507-514`):
//
//     operator runs `cargo run -p corlinman-gateway` …
//     then `swift run CorlinmanApp`. Onboard → chat → approve a tool
//     call → trigger a dev-push → notification banner appears.
//
// The shell deliberately stays light:
//
//   1. **No tenant switcher in the toolbar (yet).** The design doc
//      describes one (line 200-204) but C4's roadmap entry is one
//      reference client, not a multi-tenant operator console. The
//      tenant slug comes from `AuthStore` — switching means re-running
//      onboarding for now. A toolbar `Picker` is a small follow-up.
//   2. **Single session per launch.** Resumable sessions persist via
//      `SessionStore` (iter 5), but the shell opens a fresh
//      `sessionKey` on each launch so the iter-10 acceptance test
//      ("send → stream → memory persists across launches") has a
//      clean signal: the *previous* session's messages must show up
//      in `loadFromCache`, not a blank slate.
//   3. **No live SessionListView yet.** The sidebar is in the iter-10
//      design (line 109) but punts to a follow-up so the close-out
//      ships clean. `SessionStore` already serves the data — wiring
//      a `List` over `loadSessions(...)` is mechanical.
//
// The shell takes its dependencies through the initialiser so the
// iter-10 acceptance test can construct one without dragging the
// `@main` entry point.

import SwiftUI

import CorlinmanCore

/// Root of the post-onboarding app surface. Constructs a single
/// `ChatViewModel` against the operator's stored creds and renders
/// `ChatView`. Acceptance test reaches in here to drive a full
/// send → stream → relaunch cycle.
public struct MainShellView: View {
    @StateObject private var viewModel: ChatViewModel
    public let banner: String

    /// Build the shell from already-resolved infrastructure. The
    /// `@main` entry point passes a `URLSessionApprovalClient`; tests
    /// pass a fixture conforming to `ApprovalClient`.
    public init(
        source: ChatStreamSource,
        sessionKey: String = UUID().uuidString,
        tenantSlug: String,
        store: SessionStore?,
        approvalClient: ApprovalClient?,
        banner: String = "Corlinman macOS reference client"
    ) {
        // `_viewModel = StateObject(wrappedValue:)` is the canonical
        // pattern for injecting dependencies into a `@StateObject`;
        // direct assignment would cause SwiftUI to re-create the model
        // on every parent re-render.
        _viewModel = StateObject(wrappedValue: ChatViewModel(
            source: source,
            sessionKey: sessionKey,
            tenantSlug: tenantSlug,
            store: store,
            approvalClient: approvalClient
        ))
        self.banner = banner
    }

    public var body: some View {
        VStack(spacing: 0) {
            // Header strip — tenant + session id, so when the operator
            // has multiple windows open they can tell them apart.
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(banner)
                        .font(.headline)
                    Text("tenant \(viewModel.tenantSlug) · session \(viewModel.sessionKey.prefix(8))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .background(.thinMaterial)

            ChatView(viewModel: viewModel)
        }
        .frame(minWidth: 600, minHeight: 480)
    }

    /// Test hook — exposes the view model so the acceptance test can
    /// drive `send` / assert on `messages` without reaching through
    /// SwiftUI's render tree.
    public var _testViewModel: ChatViewModel { viewModel }
}
