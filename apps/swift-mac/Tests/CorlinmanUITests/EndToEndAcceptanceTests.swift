// Phase 4 W3 C4 iter 10 — End-to-end acceptance test.
//
// The Wave 3 acceptance criterion (`docs/roadmap/phase4-roadmap.md` §4):
//
//     The reference Swift client sends a message → gets a streamed
//     response → memory persists across launches.
//
// The design doc's iter-10 entry (`docs/design/phase4-w3-c4-design.md:507-514`)
// adds the approval round-trip and dev-push notification on top.
//
// CI cannot run a real gateway. Instead the acceptance test wires the
// production view-model (`ChatViewModel` + `SessionStore`) against a
// fixture `ChatStreamSource` that replays a hand-rolled SSE byte
// sequence. That proves every piece of the contract except the actual
// HTTP transport — which `ChatStreamTests` already covers in isolation.
//
// One scenario per acceptance row:
//
//   1. **Send + stream + persist.** Drive `ChatViewModel.send`, wait for
//      `done`, assert messages landed and `SessionStore` rows match.
//   2. **Memory persists across launches.** Construct a fresh
//      `ChatViewModel` against the *same* file-backed `SessionStore`,
//      call `loadFromCache`, assert the prior turn is rehydrated.
//   3. **Approval round-trip.** Replay an `awaiting_approval` chunk,
//      assert `pendingApproval` lands, call `resolveApproval`, assert
//      the fixture client recorded a matching POST body.
//
// The whole file is `@MainActor` because `ChatViewModel` is.

import XCTest
import Foundation

@testable import CorlinmanCore
@testable import CorlinmanUI

@MainActor
final class EndToEndAcceptanceTests: XCTestCase {

    // MARK: - Fixtures

    /// Minimal SSE chunk encoder. One JSON object per `data:` line,
    /// terminated by `data: [DONE]`. Matches the gateway's chat.rs
    /// shape close enough for the parser to be exercised end-to-end.
    private func sseTokenStream(_ tokens: [String]) -> String {
        var sse = ""
        for t in tokens {
            let body = #"{"id":"a","object":"chat.completion.chunk","model":"m","choices":[{"index":0,"delta":{"content":"\#(t)"},"finish_reason":null}]}"#
            sse += "data: \(body)\n\n"
        }
        sse += "data: [DONE]\n\n"
        return sse
    }

    /// Build a `ChatStream` that replays a hand-rolled SSE blob.
    private func sseStream(_ raw: String) -> ChatStream {
        let bytes = Array(raw.utf8)
        return ChatStream {
            AsyncThrowingStream { continuation in
                Task {
                    for byte in bytes {
                        continuation.yield(byte)
                        if byte == 0x0A { await Task.yield() }
                    }
                    continuation.finish()
                }
            }
        }
    }

    /// Fake `ChatStreamSource` — hands back a pre-baked stream. The
    /// production binding is `LiveChatStreamSource`; this fake lets us
    /// assert on send / receive without TLS.
    private final class FakeSource: ChatStreamSource, @unchecked Sendable {
        var stream: ChatStream
        var lastPrompt: String?
        var lastSessionKey: String?

        init(stream: ChatStream) { self.stream = stream }

        func openStream(for prompt: String, sessionKey: String) -> ChatStream {
            lastPrompt = prompt
            lastSessionKey = sessionKey
            return stream
        }
    }

    /// Recording approval client — captures the POST body so the
    /// approval round-trip assertion can examine what would have hit
    /// the gateway.
    private final class RecordingApprovalClient: ApprovalClient, @unchecked Sendable {
        struct Recorded: Equatable {
            let turnId: String
            let decision: ApprovalDecision
        }
        // `nonisolated(unsafe)` would be cleaner with Swift 5.10's
        // strict-concurrency vocabulary, but the project's tools
        // version is 5.9 — use NSLock for now.
        private let lock = NSLock()
        private var _calls: [Recorded] = []

        var calls: [Recorded] {
            lock.lock(); defer { lock.unlock() }
            return _calls
        }

        func submit(turnId: String, decision: ApprovalDecision) async throws -> ApprovalResponse {
            lock.lock()
            _calls.append(Recorded(turnId: turnId, decision: decision))
            lock.unlock()
            return ApprovalResponse(
                turnId: turnId,
                callId: decision.call_id,
                decision: decision.approved ? "approved" : "denied"
            )
        }
    }

    /// Use a temp file for `SessionStore` so the across-launch test
    /// can construct a *fresh* view model against the same SQLite
    /// file — `:memory:` would defeat the test.
    private func tempStorePath() throws -> String {
        let tmp = NSTemporaryDirectory()
        return (tmp as NSString).appendingPathComponent(
            "corlinman-c4-acceptance-\(UUID().uuidString).sqlite"
        )
    }

    /// Wait for the streaming task to settle. Polls `isStreaming` so
    /// we don't add `Thread.sleep` calls — main-actor reentrancy
    /// schedules quickly.
    private func waitForStreamFinish(_ vm: ChatViewModel, timeoutSec: TimeInterval = 2.0) async throws {
        let deadline = Date().addingTimeInterval(timeoutSec)
        while vm.isStreaming, Date() < deadline {
            try await Task.sleep(nanoseconds: 5_000_000)
        }
    }

    // MARK: - Acceptance row 1: send + stream + persist

    /// Drives the full happy path: open a fresh view model, send a
    /// prompt, watch tokens land, confirm SessionStore captures both
    /// rows. This is the iter-10 acceptance gate's first half.
    func test_e2e_sendStreamsAndPersistsMessages() async throws {
        let storePath = try tempStorePath()
        defer { try? FileManager.default.removeItem(atPath: storePath) }

        let store = try SessionStore(path: storePath)
        let source = FakeSource(stream: sseStream(sseTokenStream(["Hel", "lo, ", "world!"])))
        let vm = ChatViewModel(
            source: source,
            sessionKey: "e2e-sess-1",
            tenantSlug: "acme",
            store: store
        )

        vm.send("ping")
        try await waitForStreamFinish(vm)

        XCTAssertEqual(source.lastPrompt, "ping",
            "FakeSource should have observed the trimmed prompt")
        XCTAssertEqual(source.lastSessionKey, "e2e-sess-1",
            "FakeSource should have observed the session key")

        XCTAssertEqual(vm.messages.count, 2)
        XCTAssertEqual(vm.messages[0].role, .user)
        XCTAssertEqual(vm.messages[0].content, "ping")
        XCTAssertEqual(vm.messages[1].role, .assistant)
        XCTAssertEqual(vm.messages[1].content, "Hello, world!")

        // Persistence ratchet — both messages should be on disk.
        let stored = try store.loadMessages(sessionKey: "e2e-sess-1")
        XCTAssertEqual(stored.count, 2)
        XCTAssertEqual(stored.map { $0.role }, ["user", "assistant"])
        XCTAssertEqual(stored[1].content, "Hello, world!")

        let sessions = try store.loadSessions(tenantSlug: "acme")
        XCTAssertEqual(sessions.count, 1)
        XCTAssertEqual(sessions[0].sessionKey, "e2e-sess-1")
        XCTAssertEqual(sessions[0].displayTitle, "ping")
    }

    // MARK: - Acceptance row 2: memory persists across launches

    /// Iter-10 acceptance gate's second half: same SessionStore file,
    /// fresh `ChatViewModel`, `loadFromCache` rehydrates the prior
    /// turn. This is what the design doc means by "memory persists
    /// across launches".
    func test_e2e_memoryPersistsAcrossLaunches() async throws {
        let storePath = try tempStorePath()
        defer { try? FileManager.default.removeItem(atPath: storePath) }

        // ---- "First launch" — send a message and shut down.
        do {
            let store = try SessionStore(path: storePath)
            let source = FakeSource(stream: sseStream(sseTokenStream(["First", " reply"])))
            let vm = ChatViewModel(
                source: source,
                sessionKey: "session-resume",
                tenantSlug: "acme",
                store: store
            )
            vm.send("hello")
            try await waitForStreamFinish(vm)
            XCTAssertEqual(vm.messages.count, 2)
        }

        // ---- "Second launch" — fresh process, same SQLite file.
        do {
            let store = try SessionStore(path: storePath)
            let source = FakeSource(stream: sseStream(sseTokenStream([])))
            let vm = ChatViewModel(
                source: source,
                sessionKey: "session-resume",
                tenantSlug: "acme",
                store: store
            )
            // No network call — just hydrate from cache.
            vm.loadFromCache()
            XCTAssertEqual(vm.messages.count, 2,
                "Across-launch resume should rehydrate prior turn")
            XCTAssertEqual(vm.messages[0].role, .user)
            XCTAssertEqual(vm.messages[0].content, "hello")
            XCTAssertEqual(vm.messages[1].role, .assistant)
            XCTAssertEqual(vm.messages[1].content, "First reply")
        }
    }

    // MARK: - Acceptance row 3: approval round-trip

    /// Replay an `awaiting_approval` SSE event, assert `pendingApproval`
    /// surfaces, call `resolveApproval`, assert the recording
    /// `ApprovalClient` captured a body matching the wire schema at
    /// `chat_approve.rs:34-37`.
    func test_e2e_approvalRoundTripPostsDecision() async throws {
        let storePath = try tempStorePath()
        defer { try? FileManager.default.removeItem(atPath: storePath) }

        // Hand-rolled SSE blob: one token, then `event: awaiting_approval`,
        // then `[DONE]`. Mirrors the gateway shape per
        // `Models.swift:30-60`.
        let approvalPayload = #"{"turn_id":"turn-7","call_id":"call-99","plugin":"shell","tool":"run","args_preview":"ls /"}"#
        let raw = """
        data: {"id":"a","object":"chat.completion.chunk","model":"m","choices":[{"index":0,"delta":{"content":"thinking"},"finish_reason":null}]}

        event: awaiting_approval
        data: \(approvalPayload)

        data: [DONE]

        """

        let store = try SessionStore(path: storePath)
        let source = FakeSource(stream: sseStream(raw))
        let recorder = RecordingApprovalClient()
        let vm = ChatViewModel(
            source: source,
            sessionKey: "approval-sess",
            tenantSlug: "acme",
            store: store,
            approvalClient: recorder
        )

        vm.send("run ls")

        // Wait until the awaiting-approval chunk surfaces.
        let deadline = Date().addingTimeInterval(2.0)
        while vm.pendingApproval == nil, Date() < deadline {
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        guard let pending = vm.pendingApproval else {
            return XCTFail("approval prompt never surfaced")
        }
        XCTAssertEqual(pending.turnId, "turn-7")
        XCTAssertEqual(pending.id, "call-99")
        XCTAssertEqual(pending.plugin, "shell")
        XCTAssertEqual(pending.tool, "run")
        XCTAssertEqual(pending.argsPreview, "ls /")

        // Operator approves with `session` scope.
        await vm.resolveApproval(approved: true, scope: .session, denyMessage: nil)

        // Pending should be cleared on success.
        XCTAssertNil(vm.pendingApproval, "successful resolve clears the prompt")
        // Recording client must have observed the right body.
        let calls = recorder.calls
        XCTAssertEqual(calls.count, 1)
        XCTAssertEqual(calls[0].turnId, "turn-7")
        XCTAssertEqual(calls[0].decision.call_id, "call-99")
        XCTAssertEqual(calls[0].decision.approved, true)
        XCTAssertEqual(calls[0].decision.scope, .session)
        XCTAssertNil(calls[0].decision.deny_message)

        // Drain the rest of the stream so the test exits clean.
        try await waitForStreamFinish(vm)
    }

    // MARK: - Acceptance row 4: live-source bridge wires correctly

    /// The production wiring path constructs a `LiveChatStreamSource`
    /// in `CorlinmanCore` and a small bridge struct in the App layer
    /// to satisfy `ChatStreamSource` (the `CorlinmanUI` protocol).
    /// We can't run the live source here (no gateway), but we can
    /// assert the bridge compiles + accepts the same factory the App
    /// layer uses. This is a ratchet against the bridge silently
    /// breaking when either protocol shifts.
    func test_e2e_liveSourceBridgeWiresCorrectly() throws {
        struct LocalBridge: ChatStreamSource, @unchecked Sendable {
            let inner: LiveChatStreamSource
            func openStream(for prompt: String, sessionKey: String) -> ChatStream {
                inner.openStream(for: prompt, sessionKey: sessionKey)
            }
        }
        let url = URL(string: "https://gateway.example.com")!
        let live = LiveChatStreamSource(
            baseURL: url,
            bearerProvider: { "ck_test_token" }
        )
        let bridge = LocalBridge(inner: live)
        // Cast through ChatStreamSource — proves the bridge satisfies
        // the protocol the view-model depends on.
        let asProtocol: ChatStreamSource = bridge
        let _ = asProtocol.openStream(for: "ping", sessionKey: "k")
    }
}
