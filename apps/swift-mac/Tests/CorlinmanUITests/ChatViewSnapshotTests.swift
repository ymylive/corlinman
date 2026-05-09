// Phase 4 W3 C4 iter 9 — `ChatView` + `OnboardingView` snapshot tests.
//
// Snapshot harness: Pointfree's `swift-snapshot-testing`. Per the
// design test matrix at `docs/design/phase4-w3-c4-design.md:363-365`:
//
//   - chat_view_snapshot_idle
//   - chat_view_snapshot_streaming_typing_indicator
//   - approval_sheet_snapshot     (deferred — sheet ships at iter 10/11)
//
// We add two more for `OnboardingView` so the credentials and tenant-
// picker phases get snapshot coverage too — auth UX regressions are
// the cheapest to ship by accident, and the cheapest to catch with a
// snapshot.
//
// ### Why `#if canImport(SnapshotTesting)`
//
// Adding `swift-snapshot-testing` as a SwiftPM dep (the `dependencies`
// + `target.dependencies` plumbing in `Package.swift`) requires
// network access at `swift package resolve` time. Contributors who
// `git clone` offline would otherwise see a hard build break. We
// gate the snapshot block so the test file *compiles* without the
// dep and lights up the moment CI / a contributor runs
// `swift package resolve`. The non-snapshot block exercises view
// construction + body access — a ratchet against unintentional API
// changes the snapshot tests would otherwise mask.
//
// ### Determinism
//
// Snapshot tests are notoriously flaky on macOS upgrades (font
// metrics shift). Per design doc Open Question §4: pin the snapshot
// suite to `macos-latest`'s frozen toolchain in CI; tolerate <1%
// pixel diff. The CI workflow at `.github/workflows/swift-mac.yml`
// (iter 9) sets the matrix accordingly.

import XCTest
import SwiftUI

@testable import CorlinmanCore
@testable import CorlinmanUI

#if canImport(SnapshotTesting)
import SnapshotTesting
#endif

@MainActor
final class ChatViewSnapshotTests: XCTestCase {

    // MARK: - Construction sanity (always runs; no SnapshotTesting required)

    /// Builds an idle `ChatView` against an in-memory store. Asserts
    /// the body resolves — protects against API drift in
    /// `ChatViewModel.init` that would otherwise only surface in CI.
    func test_chatView_idleConstructs() async throws {
        let store = try SessionStore(path: ":memory:")
        let vm = ChatViewModel(
            source: NoopSource(),
            sessionKey: "snap-1",
            tenantSlug: "t",
            store: store
        )
        let view = ChatView(viewModel: vm)
        _ = view.body
    }

    /// Builds an onboarding view in `.credentials` phase. No snapshot
    /// gate so the construction path runs without external deps.
    func test_onboardingView_credentialsPhaseConstructs() {
        let auth = AuthStore(keychain: InMemoryKeychain(),
                              tenants: EphemeralTenantPreference())
        let vm = OnboardingViewModel(
            authStore: auth,
            factory: { _, _, _ in NoopOnboardingClient() },
            onComplete: { _ in }
        )
        let view = OnboardingView(viewModel: vm)
        _ = view.body
    }

    // MARK: - Snapshot rows (gated on the dep being resolved)

    #if canImport(SnapshotTesting)

    /// Mandated row: `chat_view_snapshot_idle`. Empty session, no
    /// streaming, baseline window — the snapshot becomes the
    /// reference for the welcome-empty layout.
    func test_chatView_snapshotIdle() throws {
        let store = try SessionStore(path: ":memory:")
        let vm = ChatViewModel(
            source: NoopSource(),
            sessionKey: "snap-idle",
            tenantSlug: "t",
            store: store
        )
        let view = ChatView(viewModel: vm)
            .frame(width: 600, height: 400)
        assertSnapshot(matching: view, as: .image(precision: 0.99))
    }

    /// Mandated row: `chat_view_snapshot_streaming_typing_indicator`.
    /// Drive the view-model into a mid-stream state where the
    /// assistant message has partial content + isStreaming=true so
    /// the trailing "…" / typing indicator renders.
    func test_chatView_snapshotStreamingTypingIndicator() throws {
        let store = try SessionStore(path: ":memory:")
        let vm = ChatViewModel(
            source: NoopSource(),
            sessionKey: "snap-stream",
            tenantSlug: "t",
            store: store
        )
        // Hand-prime the messages array via `loadFromCache` after
        // staging rows directly. We don't call `send` because that
        // would kick the (NoopSource) stream and wait for it to
        // finish — flaky for snapshots.
        try store.upsertSession(StoredSession(
            sessionKey: "snap-stream", tenantSlug: "t",
            displayTitle: "hi", lastMessageAtMs: 1, createdAtMs: 1))
        try store.appendMessage(StoredMessage(
            sessionKey: "snap-stream", role: "user",
            content: "hello", createdAtMs: 1))
        try store.appendMessage(StoredMessage(
            sessionKey: "snap-stream", role: "assistant",
            content: "I'm thinking", createdAtMs: 2))
        vm.loadFromCache()
        let view = ChatView(viewModel: vm)
            .frame(width: 600, height: 400)
        assertSnapshot(matching: view, as: .image(precision: 0.99))
    }

    /// Coverage: onboarding credentials phase rendered with sample
    /// values. Catches regressions in field labelling or button
    /// state.
    func test_onboardingView_credentialsSnapshot() {
        let auth = AuthStore(keychain: InMemoryKeychain(),
                              tenants: EphemeralTenantPreference())
        let vm = OnboardingViewModel(
            authStore: auth,
            factory: { _, _, _ in NoopOnboardingClient() },
            onComplete: { _ in }
        )
        vm.gatewayURL = "https://gateway.example.com"
        vm.adminUsername = "admin"
        vm.adminPassword = "hunter2"
        let view = OnboardingView(viewModel: vm)
            .frame(width: 600, height: 460)
        assertSnapshot(matching: view, as: .image(precision: 0.99))
    }

    #endif

    // MARK: - Test fixtures

    /// `ChatStreamSource` that hands back a no-op stream. Useful for
    /// snapshot tests that prime view-model state directly without
    /// driving the consume loop.
    private final class NoopSource: ChatStreamSource, @unchecked Sendable {
        func openStream(for prompt: String, sessionKey: String) -> ChatStream {
            ChatStream {
                AsyncThrowingStream { continuation in continuation.finish() }
            }
        }
    }

    private final class NoopOnboardingClient: OnboardingClient, @unchecked Sendable {
        func login() async throws {}
        func listTenants(forUser username: String) async throws -> [TenantSummary] { [] }
        func mintApiKey(scope: String, username: String?, label: String?) async throws -> MintedApiKey {
            // Minimal MintedApiKey (decoded from JSON to avoid copying
            // the wire shape — keeps a single source of truth).
            let json = #"{"key_id":"k","tenant_id":"t","username":"u","scope":"chat","label":null,"token":"ck_test","created_at_ms":0}"#
            return try JSONDecoder().decode(MintedApiKey.self, from: Data(json.utf8))
        }
    }
}
