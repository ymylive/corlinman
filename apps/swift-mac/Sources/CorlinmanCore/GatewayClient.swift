// Phase 4 W3 C4 iter 7 — `GatewayClient`: thin REST shim around
// the admin endpoints the onboarding flow exercises.
//
// Iter 7's job is *not* to ship the full /v1/chat/completions
// surface (the streaming side already lives in `ChatStream`). It's
// to hand `OnboardingView` a typed surface for:
//
//   - `POST /admin/auth/login`           — establish the admin session
//   - `GET  /admin/tenants?for_user=…`   — list tenants for the operator
//   - `POST /admin/api_keys`             — mint a chat-scoped bearer
//
// The expanded chat path comes back with `ChatStream.open(...)` once
// the api_key is staged in `AuthStore`.
//
// ### Why URLSession (and not `swift-async-http` or similar)
//
// URLSession is the only stack with first-class macOS support that
// also carries a real Linux port (foundation-networking). Pulling
// AsyncHTTPClient would mean adding a NIO graph for two POSTs and a
// GET — and would *still* leave the SSE-native side using URLSession.
// Single transport keeps the failure modes legible.
//
// ### Authentication
//
// Admin endpoints take HTTP Basic. The login response sets a session
// cookie that `URLSession` automatically threads through subsequent
// requests on the same shared cookie store; the client therefore
// makes no explicit attempt to extract / store that cookie. If the
// caller wants tenant-isolated sessions, they pass an `URLSession`
// configured with its own ephemeral `HTTPCookieStorage`.

import Foundation

/// Errors surfaced by the gateway client. Distinct from
/// `ChatStreamError` because the admin-REST + chat-SSE failure modes
/// don't overlap meaningfully.
public enum GatewayClientError: Error, Sendable {
    case http(status: Int, body: String?)
    case transport(underlying: Error)
    case decoding(underlying: Error)
    case invalidURL(String)
}

/// One row of the `/admin/tenants` response. Trimmed to the fields the
/// Swift onboarding UI consumes; the gateway returns more.
public struct TenantSummary: Equatable, Sendable, Decodable {
    public let id: String
    public let slug: String
    public let display_name: String?

    public init(id: String, slug: String, display_name: String?) {
        self.id = id
        self.slug = slug
        self.display_name = display_name
    }
}

/// Body for `POST /admin/api_keys`.
public struct MintApiKeyBody: Encodable, Sendable {
    public let scope: String
    public let username: String?
    public let label: String?

    public init(scope: String, username: String? = nil, label: String? = nil) {
        self.scope = scope
        self.username = username
        self.label = label
    }
}

/// Response shape from `POST /admin/api_keys`. Matches
/// `routes/admin/api_keys.rs` `MintResponse`.
public struct MintedApiKey: Decodable, Sendable {
    public let key_id: String
    public let tenant_id: String
    public let username: String
    public let scope: String
    public let label: String?
    public let token: String
    public let created_at_ms: Int64
}

/// Thin admin-REST client. Constructed with a base URL + admin Basic
/// credentials; reuses one `URLSession` so cookies persist across
/// calls. Tenants list, api_key mint, and chat-stream open all share
/// this surface so a future tenant-switch flow can call multiple
/// endpoints in sequence without re-establishing TLS.
public final class GatewayClient: @unchecked Sendable {
    public let baseURL: URL
    public let session: URLSession
    private let basicAuth: String?

    /// Construct a client. `adminCredentials == nil` is used by tests
    /// that inject `URLProtocol`-backed mocks — the basic-auth header
    /// is then their problem to assert on.
    public init(
        baseURL: URL,
        adminCredentials: (username: String, password: String)? = nil,
        session: URLSession = .shared
    ) {
        self.baseURL = baseURL
        self.session = session
        if let creds = adminCredentials,
           let raw = "\(creds.username):\(creds.password)".data(using: .utf8) {
            self.basicAuth = "Basic " + raw.base64EncodedString()
        } else {
            self.basicAuth = nil
        }
    }

    /// `POST /admin/auth/login`. Empty body — Basic header carries
    /// the credentials. Establishes the session cookie that subsequent
    /// admin calls rely on.
    public func adminLogin() async throws {
        let req = try makeRequest(path: "/admin/auth/login", method: "POST")
        let (_, response) = try await sendDataRaw(req, expectedStatuses: [200, 204])
        // Cookie storage absorbs the Set-Cookie header for free.
        _ = response
    }

    /// `GET /admin/tenants?for_user=<username>`. Returns the tenants
    /// the authenticated operator has access to, in display order.
    public func listTenants(forUser username: String) async throws -> [TenantSummary] {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("/admin/tenants"),
            resolvingAgainstBaseURL: false
        )
        components?.queryItems = [URLQueryItem(name: "for_user", value: username)]
        guard let url = components?.url else {
            throw GatewayClientError.invalidURL("/admin/tenants")
        }
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        applyAuth(&req)
        let (data, _) = try await sendDataRaw(req, expectedStatuses: [200])

        // Tolerate two server shapes: `{ "tenants": [...] }` and the
        // raw `[...]` form `routes/admin/tenants.rs` returns
        // historically. Whichever lands, decode and return.
        struct Wrapper: Decodable { let tenants: [TenantSummary] }
        if let wrapped = try? JSONDecoder().decode(Wrapper.self, from: data) {
            return wrapped.tenants
        }
        do {
            return try JSONDecoder().decode([TenantSummary].self, from: data)
        } catch {
            throw GatewayClientError.decoding(underlying: error)
        }
    }

    /// `POST /admin/api_keys` — mint a chat-scoped bearer. Cleartext
    /// `token` is returned exactly once; caller is responsible for
    /// stuffing it into `AuthStore`.
    public func mintApiKey(scope: String, username: String?, label: String?) async throws -> MintedApiKey {
        var req = try makeRequest(path: "/admin/api_keys", method: "POST")
        let body = MintApiKeyBody(scope: scope, username: username, label: label)
        req.httpBody = try JSONEncoder().encode(body)
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (data, _) = try await sendDataRaw(req, expectedStatuses: [200, 201])
        do {
            return try JSONDecoder().decode(MintedApiKey.self, from: data)
        } catch {
            throw GatewayClientError.decoding(underlying: error)
        }
    }

    // MARK: - Internals

    private func makeRequest(path: String, method: String) throws -> URLRequest {
        let url = baseURL.appendingPathComponent(path)
        var req = URLRequest(url: url)
        req.httpMethod = method
        applyAuth(&req)
        return req
    }

    private func applyAuth(_ req: inout URLRequest) {
        if let basic = basicAuth {
            req.setValue(basic, forHTTPHeaderField: "Authorization")
        }
    }

    private func sendDataRaw(
        _ req: URLRequest,
        expectedStatuses: [Int]
    ) async throws -> (Data, HTTPURLResponse) {
        do {
            let (data, response) = try await session.data(for: req)
            guard let http = response as? HTTPURLResponse else {
                throw GatewayClientError.http(status: 0, body: nil)
            }
            if !expectedStatuses.contains(http.statusCode) {
                let body = String(data: data, encoding: .utf8)
                throw GatewayClientError.http(status: http.statusCode, body: body)
            }
            return (data, http)
        } catch let err as GatewayClientError {
            throw err
        } catch {
            throw GatewayClientError.transport(underlying: error)
        }
    }
}
