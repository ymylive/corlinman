// Phase 4 W3 C4 iter 10 — `ApprovalClient`: typed surface for the
// per-turn approval relay at
// `POST /v1/chat/completions/:turn_id/approve`
// (`rust/crates/corlinman-gateway/src/routes/chat_approve.rs:14-38`).
//
// Why a separate client (vs. folding into `GatewayClient`):
//
//   1. **Auth profile differs.** `GatewayClient` carries Basic-auth
//      credentials for the admin endpoints; this surface needs the
//      `chat`-scoped Bearer token from `AuthStore.apiKey`. Mixing the
//      two in one client invites accidental header bleed when a
//      future caller forgets to clear the Basic header before hitting
//      a `/v1/*` route.
//   2. **Lifecycle differs.** Admin REST is a one-shot during
//      onboarding; the approval relay is hot-path during a streaming
//      turn. Keeping them split lets `ApprovalClient` reuse the same
//      `URLSession` `ChatStream.open` does (cookie-free, single TLS
//      connection), without dragging cookie storage in.
//   3. **Mockability.** `ChatViewModelTests` injects a fake conformance;
//      forcing a single `GatewayClient` would drag the admin Basic
//      header parsing through every test even when the test doesn't
//      care about admin paths.
//
// The shape mirrors `chat_approve.rs:34-37` exactly (`call_id`,
// `approved`, `scope`, `deny_message`). When the gateway grows
// scope-tracking past iter 3's stub, the body remains forward-compat:
// new optional fields can be added without breaking older clients.

import Foundation

/// One operator decision against an `awaiting_approval` chunk. Mirrors
/// the body of `POST /v1/chat/completions/:turn_id/approve` —
/// `chat_approve.rs:34-37`.
public struct ApprovalDecision: Encodable, Equatable, Sendable {
    /// Scope of the approval — server treats `session` / `always` as
    /// `once` until iter-3-stub-scope-tracking lands. Forward-compat
    /// today; remembered properly post-stub.
    public enum Scope: String, Codable, Sendable {
        case once, session, always
    }

    public let call_id: String
    public let approved: Bool
    public let scope: Scope
    public let deny_message: String?

    public init(
        callId: String,
        approved: Bool,
        scope: Scope,
        denyMessage: String? = nil
    ) {
        self.call_id = callId
        self.approved = approved
        self.scope = scope
        self.deny_message = denyMessage
    }
}

/// Server response on a successful approve POST. Mirrors `chat_approve.rs:30-32`.
public struct ApprovalResponse: Decodable, Equatable, Sendable {
    public let turn_id: String
    public let call_id: String
    public let decision: String      // "approved" | "denied"

    public init(turnId: String, callId: String, decision: String) {
        self.turn_id = turnId
        self.call_id = callId
        self.decision = decision
    }
}

/// Errors surfaced by `ApprovalClient`. Reuses the same buckets as
/// `GatewayClientError` so callers see a consistent error taxonomy.
public enum ApprovalClientError: Error, Sendable {
    case http(status: Int, body: String?)
    case transport(underlying: Error)
    case decoding(underlying: Error)
    case missingBearer
    case invalidURL(String)
}

/// Pluggable surface so `ApprovalSheet`'s view model can be tested
/// without spinning up URLSession. The production binding is
/// `URLSessionApprovalClient`; tests inject a fake.
public protocol ApprovalClient: Sendable {
    /// Send a decision for one in-flight tool call. Returns the server
    /// echo on success; throws on transport / decoding / non-2xx.
    func submit(turnId: String, decision: ApprovalDecision) async throws -> ApprovalResponse
}

/// Production binding. Holds the gateway base URL + a closure that
/// supplies the current Bearer token (read from `AuthStore` at call
/// time so a tenant switch picks up the new key without rewiring).
public final class URLSessionApprovalClient: ApprovalClient, @unchecked Sendable {
    public let baseURL: URL
    public let session: URLSession
    private let bearerProvider: @Sendable () -> String?

    public init(
        baseURL: URL,
        session: URLSession = .shared,
        bearerProvider: @Sendable @escaping () -> String?
    ) {
        self.baseURL = baseURL
        self.session = session
        self.bearerProvider = bearerProvider
    }

    public func submit(
        turnId: String,
        decision: ApprovalDecision
    ) async throws -> ApprovalResponse {
        guard let bearer = bearerProvider(), !bearer.isEmpty else {
            throw ApprovalClientError.missingBearer
        }
        guard !turnId.isEmpty else {
            throw ApprovalClientError.invalidURL("/v1/chat/completions//approve")
        }
        let path = "/v1/chat/completions/\(turnId)/approve"
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            throw ApprovalClientError.invalidURL(path)
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization")
        do {
            req.httpBody = try JSONEncoder().encode(decision)
        } catch {
            throw ApprovalClientError.decoding(underlying: error)
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: req)
        } catch {
            throw ApprovalClientError.transport(underlying: error)
        }
        guard let http = response as? HTTPURLResponse else {
            throw ApprovalClientError.http(status: 0, body: nil)
        }
        guard (200...299).contains(http.statusCode) else {
            throw ApprovalClientError.http(
                status: http.statusCode,
                body: String(data: data, encoding: .utf8)
            )
        }
        do {
            return try JSONDecoder().decode(ApprovalResponse.self, from: data)
        } catch {
            throw ApprovalClientError.decoding(underlying: error)
        }
    }
}
