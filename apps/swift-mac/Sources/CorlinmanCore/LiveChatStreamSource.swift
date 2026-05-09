// Phase 4 W3 C4 iter 10 — `LiveChatStreamSource`: production
// implementation of the `ChatStreamSource` protocol the view model
// consumes.
//
// `ChatStreamSource` lives in `CorlinmanUI` because the protocol's only
// concrete consumer is `ChatViewModel`, but the *production* binding
// is networking and therefore belongs in `CorlinmanCore`. Rather than
// flip the protocol's residency (which would force `CorlinmanCore` to
// know about UI types), we declare a parallel protocol here in Core
// and let the App layer write a 4-line bridge. That keeps the layering
// clean: Core has zero `import SwiftUI`, UI has zero networking, App
// is the only place both meet.
//
// One constructor argument carries the operator's choices:
//   - `baseURL`            — gateway root, e.g. `https://gw.example.com`.
//   - `bearerProvider`     — closure resolving the chat-scoped api_key
//                             at call time (same pattern
//                             `URLSessionApprovalClient` uses).
//   - `model`              — model id requested in the request body.
//   - `session`            — `URLSession` injection point for tests.
//
// On `openStream` we build the OpenAI-style `chat.completions` POST
// the gateway expects (`routes/chat.rs:1` shape) with `stream: true`,
// hand it to `ChatStream.open(...)`, and let the iter-4 SSE parser
// take over. No business logic lives here — the view model is the
// place where chunks become UI state.

import Foundation

/// What `LiveChatStreamSource` produces — same shape `ChatStreamSource`
/// in `CorlinmanUI` declares. We can't `import CorlinmanUI` here (UI
/// → Core, not Core → UI), so the App layer writes a 1-line bridge:
///
/// ```swift
/// extension LiveChatStreamSource: ChatStreamSource {}   // bridges naming
/// ```
public protocol CoreChatStreamSource: Sendable {
    func openStream(for prompt: String, sessionKey: String) -> ChatStream
}

/// Configures + opens streaming chat completions against a real
/// gateway. Stateless across calls — instances are cheap to create
/// per session if the operator wants per-session URLSessions.
public final class LiveChatStreamSource: CoreChatStreamSource, @unchecked Sendable {
    public let baseURL: URL
    public let model: String
    public let session: URLSession
    private let bearerProvider: @Sendable () -> String?

    public init(
        baseURL: URL,
        model: String = "corlinman/chat",
        session: URLSession = .shared,
        bearerProvider: @Sendable @escaping () -> String?
    ) {
        self.baseURL = baseURL
        self.model = model
        self.session = session
        self.bearerProvider = bearerProvider
    }

    public func openStream(for prompt: String, sessionKey: String) -> ChatStream {
        // Construct the request lazily so the bearer is read on each
        // openStream call (tenant-switch / re-mint cases).
        let url = baseURL.appendingPathComponent("/v1/chat/completions")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        if let bearer = bearerProvider(), !bearer.isEmpty {
            req.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization")
        }
        // OpenAI-compatible request body. `session_key` is a corlinman
        // extension the gateway maps to its session table; documented
        // in the wire-protocol writeup at `apps/swift-mac/docs/wire-protocol.md`.
        let body: [String: Any] = [
            "model": model,
            "stream": true,
            "messages": [
                ["role": "user", "content": prompt],
            ],
            "session_key": sessionKey,
        ]
        if let json = try? JSONSerialization.data(withJSONObject: body) {
            req.httpBody = json
        }
        return ChatStream.open(request: req, session: session)
    }
}
