// Phase 4 W3 C4 iter 6 — `ChatViewModel` unit tests.
//
// We avoid SwiftUI rendering and instead drive the view-model
// directly: feed it a `ChatStreamSource` fake that replays a hand-
// rolled SSE byte sequence, then assert on `messages` /
// `isStreaming` / persisted rows.
//
// Snapshot-style tests for `ChatView` itself land at iter 8 once
// `swift-snapshot-testing` is wired (CI gate).
//
// These tests are scaffolded — `swift test` is unrunnable in the
// CommandLineTools-only sandbox (no XCTest). They run on macOS CI.

import XCTest

@testable import CorlinmanCore
@testable import CorlinmanUI

@MainActor
final class ChatViewModelTests: XCTestCase {

    // MARK: - Helpers

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

    private func tokenStream(_ tokens: [String]) -> ChatStream {
        var sse = ""
        for t in tokens {
            let body = #"{"id":"a","object":"chat.completion.chunk","model":"m","choices":[{"index":0,"delta":{"content":"\#(t)"},"finish_reason":null}]}"#
            sse += "data: \(body)\n\n"
        }
        sse += "data: [DONE]\n\n"
        return sseStream(sse)
    }

    private final class FakeSource: ChatStreamSource, @unchecked Sendable {
        let stream: ChatStream
        init(stream: ChatStream) { self.stream = stream }
        func openStream(for prompt: String, sessionKey: String) -> ChatStream { stream }
    }

    // MARK: - Send appends user + streams assistant

    func test_send_streamsAssistantTokensIntoLastMessage() async throws {
        let source = FakeSource(stream: tokenStream(["Hel", "lo"]))
        let store = try SessionStore(path: ":memory:")
        let vm = ChatViewModel(source: source, sessionKey: "s-1",
                               tenantSlug: "t", store: store)
        vm.send("hi")
        // Drain by polling — the streaming task posts to the main
        // actor; once `isStreaming` flips false we know consume()
        // returned.
        let timeout = Date().addingTimeInterval(2.0)
        while vm.isStreaming, Date() < timeout {
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertFalse(vm.isStreaming, "stream should finish within 2s")
        XCTAssertEqual(vm.messages.count, 2)
        XCTAssertEqual(vm.messages[0].role, .user)
        XCTAssertEqual(vm.messages[0].content, "hi")
        XCTAssertEqual(vm.messages[1].role, .assistant)
        XCTAssertEqual(vm.messages[1].content, "Hello")
        XCTAssertFalse(vm.messages[1].isStreaming)
    }

    // MARK: - Cancel mid-stream

    func test_cancelStreaming_stopsConsumerAndUnflagsMessage() async throws {
        // Stream that emits one chunk then stalls forever — the
        // cancel call has to break us out.
        let source = FakeSource(stream: ChatStream {
            AsyncThrowingStream { continuation in
                Task {
                    let preamble = "data: {\"id\":\"a\",\"object\":\"chat.completion.chunk\",\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hi\"},\"finish_reason\":null}]}\n\n"
                    for byte in preamble.utf8 { continuation.yield(byte) }
                    try? await Task.sleep(nanoseconds: 10_000_000_000)
                    continuation.finish()
                }
            }
        })
        let vm = ChatViewModel(source: source, sessionKey: "s",
                               tenantSlug: "t")
        vm.send("hello")
        // Wait for the first chunk to land.
        let arrived = Date().addingTimeInterval(2.0)
        while vm.messages.last?.content.isEmpty != false, Date() < arrived {
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        vm.cancelStreaming()
        // Brief settle.
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertFalse(vm.isStreaming)
        XCTAssertFalse(vm.messages.last?.isStreaming ?? true)
        XCTAssertEqual(vm.messages.last?.content, "hi")
    }

    // MARK: - Persistence round-trip

    func test_send_persistsUserAndAssistantMessages() async throws {
        let store = try SessionStore(path: ":memory:")
        let source = FakeSource(stream: tokenStream(["A"]))
        let vm = ChatViewModel(source: source, sessionKey: "k",
                               tenantSlug: "t", store: store)
        vm.send("ping")
        let timeout = Date().addingTimeInterval(2.0)
        while vm.isStreaming, Date() < timeout {
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        let stored = try store.loadMessages(sessionKey: "k")
        XCTAssertEqual(stored.count, 2)
        XCTAssertEqual(stored[0].role, "user")
        XCTAssertEqual(stored[0].content, "ping")
        XCTAssertEqual(stored[1].role, "assistant")
        XCTAssertEqual(stored[1].content, "A")
        let sessions = try store.loadSessions(tenantSlug: "t")
        XCTAssertEqual(sessions.count, 1)
        XCTAssertEqual(sessions[0].displayTitle, "ping")
    }

    // MARK: - loadFromCache hydrates messages

    func test_loadFromCache_hydratesPriorMessages() async throws {
        let store = try SessionStore(path: ":memory:")
        try store.upsertSession(StoredSession(
            sessionKey: "k", tenantSlug: "t",
            displayTitle: "earlier", lastMessageAtMs: 1, createdAtMs: 1))
        try store.appendMessage(StoredMessage(
            sessionKey: "k", role: "user",
            content: "old prompt", createdAtMs: 1))
        try store.appendMessage(StoredMessage(
            sessionKey: "k", role: "assistant",
            content: "old reply", createdAtMs: 2))
        let vm = ChatViewModel(
            source: FakeSource(stream: tokenStream([])),
            sessionKey: "k", tenantSlug: "t", store: store
        )
        vm.loadFromCache()
        XCTAssertEqual(vm.messages.count, 2)
        XCTAssertEqual(vm.messages[0].role, .user)
        XCTAssertEqual(vm.messages[0].content, "old prompt")
        XCTAssertEqual(vm.messages[1].role, .assistant)
        XCTAssertEqual(vm.messages[1].content, "old reply")
    }
}
