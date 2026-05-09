// Phase 4 W3 C4 iter 4 — `ChatStream`: SSE → `AsyncSequence<ChatChunk>`.
//
// The gateway speaks Server-Sent Events for streaming chat
// (`rust/crates/corlinman-gateway/src/routes/chat.rs:1-22`), but Apple
// has no built-in `EventSource` on macOS. We frame manually off
// `URLSession.bytes(for:)` — already an `AsyncSequence<UInt8>` — so the
// stream behaves like any other Swift async iterator. Cancellation
// flows naturally: a cancelled task tears down the URLSession data
// task, which surfaces as an HTTP-level disconnect to the gateway,
// which propagates to the agent as `Cancel`
// (`agent.proto:94-97`). No explicit cancel RPC needed.
//
// Framing rules (subset of RFC 8895 SSE we use):
//   - Lines are terminated by `\n` (the gateway happens to send LF,
//     but we tolerate `\r\n` for proxy-tolerance).
//   - A blank line (`\n\n`) flushes the current event.
//   - `data: …` accumulates the data buffer (joined by `\n` if multi-line).
//   - `event: …` sets the event name (default empty/`message`).
//   - `id: …`, `retry: …`, comments (`:` prefix), unknown fields → ignored.
//   - The terminal data payload `[DONE]` closes the stream.
//
// We keep parsing self-contained (no external SSE library) — the
// 60-line state machine here is auditable, and pulling in
// `LDSwiftEventSource` would mean dragging Carthage / a second package
// resolver. The cost of NIH on this scale is dwarfed by the cost of
// debugging a cross-language SSE bug we don't own.

import Foundation

/// Errors that can fail an in-flight chat stream. Distinct from
/// `URLError` so consumers can pattern-match without the `errno`
/// shrapnel.
public enum ChatStreamError: Error, Sendable {
    /// Underlying transport failure (DNS, TLS, connection reset).
    case transport(underlying: Error)
    /// Server returned a non-2xx response.
    case http(status: Int, body: String?)
    /// SSE framing was malformed beyond recovery.
    case malformedFrame(reason: String)
}

/// Async sequence that yields one `ChatChunk` per SSE event the
/// gateway sends. Backed by a generic `AsyncSequence` of bytes so unit
/// tests can swap a fixture in without spinning up URLSession.
public struct ChatStream: AsyncSequence, Sendable {
    public typealias Element = ChatChunk

    private let byteSource: @Sendable () -> AsyncThrowingStream<UInt8, Error>

    /// Build a stream from a closure that opens the byte source on
    /// demand. The closure is invoked once per `makeAsyncIterator()`
    /// call so `for try await` re-iteration restarts the network task
    /// rather than reusing a drained buffer.
    public init(byteSource: @Sendable @escaping () -> AsyncThrowingStream<UInt8, Error>) {
        self.byteSource = byteSource
    }

    public func makeAsyncIterator() -> Iterator {
        Iterator(bytes: byteSource(), decoder: ChatChunkDecoder())
    }

    public struct Iterator: AsyncIteratorProtocol {
        private var bytes: AsyncThrowingStream<UInt8, Error>
        private var byteIterator: AsyncThrowingStream<UInt8, Error>.AsyncIterator
        private let decoder: ChatChunkDecoder
        private var lineBuffer: [UInt8] = []
        private var dataBuffer: [String] = []
        private var eventName: String? = nil
        private var doneSeen = false

        init(bytes: AsyncThrowingStream<UInt8, Error>, decoder: ChatChunkDecoder) {
            self.bytes = bytes
            self.byteIterator = bytes.makeAsyncIterator()
            self.decoder = decoder
        }

        public mutating func next() async throws -> ChatChunk? {
            if doneSeen { return nil }
            while true {
                let byte: UInt8?
                do {
                    byte = try await byteIterator.next()
                } catch is CancellationError {
                    return nil
                } catch {
                    throw ChatStreamError.transport(underlying: error)
                }
                guard let b = byte else {
                    // Stream ended without `[DONE]` — surface whatever
                    // we have and stop. A well-behaved gateway always
                    // sends the sentinel, but proxies sometimes drop
                    // it on idle timeout, and tearing the consumer
                    // down with an error in that case would surface
                    // false positives in the UI.
                    if let chunk = try flushPendingEvent() {
                        return chunk
                    }
                    return nil
                }
                if b == 0x0A {
                    // LF — line boundary.
                    let line = String(bytes: lineBuffer, encoding: .utf8) ?? ""
                    lineBuffer.removeAll(keepingCapacity: true)
                    if line.isEmpty || line == "\r" {
                        // Blank line → dispatch the accumulated event.
                        if let chunk = try flushPendingEvent() {
                            return chunk
                        }
                        // No payload (e.g. keep-alive comment) — keep reading.
                        continue
                    }
                    try absorbLine(line)
                } else {
                    lineBuffer.append(b)
                }
            }
        }

        /// Try to emit a `ChatChunk` from the currently buffered
        /// `data:` lines. Resets the buffer regardless. Returns `nil`
        /// when the event was a non-yielding signal (keep-alive).
        private mutating func flushPendingEvent() throws -> ChatChunk? {
            defer {
                dataBuffer.removeAll(keepingCapacity: true)
                eventName = nil
            }
            guard !dataBuffer.isEmpty else { return nil }
            let payload = dataBuffer.joined(separator: "\n")
            let chunk = try decoder.decode(dataLine: payload, eventName: eventName)
            if case .done = chunk { doneSeen = true }
            return chunk
        }

        /// Absorb a single non-blank line into the current event
        /// state. Unknown fields are intentionally silent — SSE's
        /// extensibility model says ignore-unknown.
        private mutating func absorbLine(_ raw: String) throws {
            // Strip trailing CR for CRLF tolerance.
            let line = raw.hasSuffix("\r") ? String(raw.dropLast()) : raw
            if line.first == ":" {
                // Comment / keep-alive — ignore by spec.
                return
            }
            guard let colonIndex = line.firstIndex(of: ":") else {
                // Field with no value → empty value per SSE spec.
                return
            }
            let field = String(line[..<colonIndex])
            var value = String(line[line.index(after: colonIndex)...])
            if value.first == " " { value.removeFirst() }

            switch field {
            case "data":
                dataBuffer.append(value)
            case "event":
                eventName = value
            case "id", "retry":
                // Captured by the SSE spec but not consumed here.
                break
            default:
                break
            }
        }
    }
}

// MARK: - URLSession-backed factory

extension ChatStream {
    /// Open a streaming chat against the gateway. The request must
    /// already have its `Authorization: Bearer …` header set (the
    /// caller owns auth — see `AuthStore`). On non-2xx the returned
    /// stream's first `next()` throws `.http`.
    ///
    /// `session` is injectable so tests can use an `URLProtocol`-backed
    /// configuration. Production passes `URLSession.shared`.
    public static func open(
        request: URLRequest,
        session: URLSession = .shared
    ) -> ChatStream {
        ChatStream {
            AsyncThrowingStream { continuation in
                let task = Task {
                    do {
                        let (asyncBytes, response) = try await session.bytes(for: request)
                        if let http = response as? HTTPURLResponse,
                           !(200...299).contains(http.statusCode) {
                            // Drain a small body for diagnostics, then
                            // surface as `.http`. We deliberately cap
                            // the body read at 4KB so a misbehaving
                            // server can't pin us reading megabytes
                            // of HTML 502 page.
                            var collected = Data()
                            for try await byte in asyncBytes {
                                collected.append(byte)
                                if collected.count >= 4_096 { break }
                            }
                            let body = String(data: collected, encoding: .utf8)
                            continuation.finish(throwing: ChatStreamError.http(
                                status: http.statusCode,
                                body: body
                            ))
                            return
                        }
                        for try await byte in asyncBytes {
                            try Task.checkCancellation()
                            continuation.yield(byte)
                        }
                        continuation.finish()
                    } catch is CancellationError {
                        continuation.finish()
                    } catch {
                        continuation.finish(throwing: error)
                    }
                }
                continuation.onTermination = { _ in
                    task.cancel()
                }
            }
        }
    }
}
