// Phase 4 W3 C4 iter 4 — `ChatStream` SSE parser tests.
//
// We test the parser without going through URLSession by exposing
// `ChatStream` over an injected byte source. Each test feeds a hand-
// rolled SSE buffer and asserts the emitted `ChatChunk` sequence.
//
// Mandated rows (per design doc §"Test matrix"):
//   - `chat_stream_parses_token_deltas`
//   - `chat_stream_parses_tool_call_deltas`
//   - `chat_stream_handles_done_sentinel`
//   - `chat_stream_propagates_cancellation`
// We add three more for coverage we'd otherwise be flying blind on:
//   - keep-alive comments don't yield
//   - awaiting_approval custom event decodes
//   - finish_reason chunk produces `.done` with the reason

import XCTest

@testable import CorlinmanCore

final class ChatStreamTests: XCTestCase {

    // MARK: - Helpers

    /// Build a `ChatStream` whose byte source replays the given SSE
    /// payload byte-by-byte. Lets us drive the iterator deterministically.
    private func stream(from sse: String) -> ChatStream {
        let bytes = Array(sse.utf8)
        return ChatStream {
            AsyncThrowingStream { continuation in
                Task {
                    for byte in bytes {
                        continuation.yield(byte)
                        // Yield occasionally so cancellation tests can wedge in.
                        if byte == 0x0A { await Task.yield() }
                    }
                    continuation.finish()
                }
            }
        }
    }

    /// Drain a stream into an array, capping at `maxChunks` to avoid
    /// runaway tests if the parser regresses into an infinite loop.
    private func collect(_ stream: ChatStream, maxChunks: Int = 32) async throws -> [ChatChunk] {
        var out: [ChatChunk] = []
        for try await chunk in stream {
            out.append(chunk)
            if out.count >= maxChunks { break }
        }
        return out
    }

    // MARK: - Token deltas (mandated)

    func test_chatStream_parsesTokenDeltas() async throws {
        let sse = """
        data: {"id":"chatcmpl-1","object":"chat.completion.chunk","model":"gpt-4o","choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"},"finish_reason":null}]}

        data: {"id":"chatcmpl-1","object":"chat.completion.chunk","model":"gpt-4o","choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":null}]}

        data: [DONE]


        """
        let chunks = try await collect(stream(from: sse))
        XCTAssertEqual(chunks.count, 3)
        guard case let .tokenDelta(_, _, c1) = chunks[0],
              case let .tokenDelta(_, _, c2) = chunks[1],
              case .done = chunks[2]
        else {
            return XCTFail("unexpected chunk shape: \(chunks)")
        }
        XCTAssertEqual(c1, "Hel")
        XCTAssertEqual(c2, "lo")
    }

    // MARK: - Tool-call deltas (mandated)

    func test_chatStream_parsesToolCallDeltas() async throws {
        // First frame: name + start of arguments. Second frame:
        // arguments tail. Mirrors how OpenAI emits split function args.
        let sse = """
        data: {"id":"x","object":"chat.completion.chunk","model":"gpt-4o","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_a","type":"function","function":{"name":"do_thing","arguments":"{\\"q\\":"}}]},"finish_reason":null}]}

        data: {"id":"x","object":"chat.completion.chunk","model":"gpt-4o","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"hi\\"}"}}]},"finish_reason":null}]}

        data: [DONE]


        """
        let chunks = try await collect(stream(from: sse))
        XCTAssertEqual(chunks.count, 3)
        guard case let .toolCallDelta(_, _, idx1, callId1, name1, args1) = chunks[0],
              case let .toolCallDelta(_, _, idx2, _, _, args2) = chunks[1]
        else {
            return XCTFail("expected two toolCallDelta chunks: \(chunks)")
        }
        XCTAssertEqual(idx1, 0)
        XCTAssertEqual(idx2, 0)
        XCTAssertEqual(callId1, "call_a")
        XCTAssertEqual(name1, "do_thing")
        XCTAssertEqual(args1, "{\"q\":")
        XCTAssertEqual(args2, "\"hi\"}")
    }

    // MARK: - DONE sentinel (mandated)

    func test_chatStream_handlesDoneSentinel() async throws {
        let sse = "data: [DONE]\n\n"
        let chunks = try await collect(stream(from: sse))
        XCTAssertEqual(chunks.count, 1)
        guard case let .done(reason) = chunks[0] else {
            return XCTFail("expected .done; got \(chunks)")
        }
        // `[DONE]` carries no finish_reason; that's set by the
        // preceding `finish_reason` chunk (see the next test).
        XCTAssertNil(reason)
    }

    func test_chatStream_finishReasonChunkProducesDone() async throws {
        let sse = """
        data: {"id":"y","object":"chat.completion.chunk","model":"gpt-4o","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

        data: [DONE]


        """
        let chunks = try await collect(stream(from: sse))
        XCTAssertEqual(chunks.count, 2)
        guard case let .done(reason) = chunks[0] else {
            return XCTFail("expected first chunk .done; got \(chunks[0])")
        }
        XCTAssertEqual(reason, "stop")
    }

    // MARK: - Cancellation (mandated)

    func test_chatStream_propagatesCancellation() async throws {
        // A byte source that never finishes — only Task cancellation
        // breaks us out. We start consuming, then cancel after the
        // first chunk arrives.
        let neverEndingSource: @Sendable () -> AsyncThrowingStream<UInt8, Error> = {
            AsyncThrowingStream { continuation in
                Task {
                    let preamble = "data: {\"id\":\"a\",\"object\":\"chat.completion.chunk\",\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hi\"},\"finish_reason\":null}]}\n\n"
                    for byte in preamble.utf8 {
                        continuation.yield(byte)
                    }
                    // Park forever — the test has to cancel.
                    try? await Task.sleep(nanoseconds: 10_000_000_000)
                    continuation.finish()
                }
            }
        }
        let stream = ChatStream(byteSource: neverEndingSource)

        let firstChunkSeen = expectation(description: "first chunk seen")
        let cancelled = expectation(description: "iterator returns nil after cancel")
        let task = Task {
            var iter = stream.makeAsyncIterator()
            let first = try await iter.next()
            XCTAssertNotNil(first)
            firstChunkSeen.fulfill()
            // Park until cancelled.
            do {
                while try await iter.next() != nil { /* spin */ }
                cancelled.fulfill()
            } catch is CancellationError {
                cancelled.fulfill()
            } catch {
                XCTFail("unexpected error: \(error)")
            }
        }
        await fulfillment(of: [firstChunkSeen], timeout: 2.0)
        task.cancel()
        await fulfillment(of: [cancelled], timeout: 2.0)
    }

    // MARK: - Keep-alive comments

    func test_chatStream_keepaliveCommentsDoNotYield() async throws {
        let sse = """
        : keepalive 1

        : keepalive 2

        data: {"id":"x","object":"chat.completion.chunk","model":"m","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}

        data: [DONE]


        """
        let chunks = try await collect(stream(from: sse))
        XCTAssertEqual(chunks.count, 2)  // one tokenDelta + one done
        guard case .tokenDelta = chunks[0], case .done = chunks[1] else {
            return XCTFail("expected tokenDelta then done, got \(chunks)")
        }
    }

    // MARK: - Custom approval frame

    func test_chatStream_awaitingApprovalEventDecodes() async throws {
        let sse = """
        event: awaiting_approval
        data: {"turn_id":"t-1","call_id":"call_x","plugin":"shell","tool":"run","args_preview":"ls -la"}


        """
        let chunks = try await collect(stream(from: sse))
        XCTAssertEqual(chunks.count, 1)
        guard case let .awaitingApproval(turnId, callId, plugin, tool, preview) = chunks[0] else {
            return XCTFail("expected awaitingApproval; got \(chunks)")
        }
        XCTAssertEqual(turnId, "t-1")
        XCTAssertEqual(callId, "call_x")
        XCTAssertEqual(plugin, "shell")
        XCTAssertEqual(tool, "run")
        XCTAssertEqual(preview, "ls -la")
    }
}
