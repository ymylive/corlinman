// Phase 4 W3 C4 iter 7 — `OnboardingViewModel`: drives the
// first-launch flow rendered by `OnboardingView`.
//
// Three phases run in sequence:
//
//   .credentials → submitCredentials() →
//   .tenants     → confirmTenant() →
//   .minting     → (auto)            →
//   .done        → onComplete() callback to App layer
//
// Errors in any phase set `lastError` and bounce the operator back
// to `.credentials` so they can fix the URL / password without
// losing the tenant selection. Singleton tenant lists skip
// `.tenants` and go straight to mint.
//
// The view model is `@MainActor` — same rationale as `ChatViewModel`.
// All persistence side-effects route through `AuthStore` so callers
// in tests can inject an in-memory keychain and assert on the saved
// values directly.

import Foundation
#if canImport(SwiftUI)
import SwiftUI
#endif

import CorlinmanCore

/// Pluggable surface so tests can fake the network without spinning
/// up an HTTP server. Production wires `LiveOnboardingClient` which
/// delegates to `GatewayClient`.
public protocol OnboardingClient: Sendable {
    func login() async throws
    func listTenants(forUser username: String) async throws -> [TenantSummary]
    func mintApiKey(scope: String, username: String?, label: String?) async throws -> MintedApiKey
}

/// Real implementation backed by `GatewayClient`. The view model
/// recreates one of these on each `.credentials` submit because the
/// base URL + admin Basic header are baked into the client at init.
public struct LiveOnboardingClient: OnboardingClient {
    public let client: GatewayClient

    public init(client: GatewayClient) { self.client = client }

    public func login() async throws { try await client.adminLogin() }

    public func listTenants(forUser username: String) async throws -> [TenantSummary] {
        try await client.listTenants(forUser: username)
    }

    public func mintApiKey(scope: String, username: String?, label: String?) async throws -> MintedApiKey {
        try await client.mintApiKey(scope: scope, username: username, label: label)
    }
}

/// Factory closure: given (baseURL, username, password) produce
/// an `OnboardingClient`. `LiveOnboardingFactory` is the production
/// wiring; tests pass a closure that returns an in-memory fake.
public typealias OnboardingClientFactory = @Sendable (URL, String, String) -> OnboardingClient

/// Default factory that builds a `LiveOnboardingClient` over
/// `GatewayClient`. Tests don't import this — they build their own.
public let LiveOnboardingFactory: OnboardingClientFactory = { url, user, pass in
    LiveOnboardingClient(client: GatewayClient(
        baseURL: url,
        adminCredentials: (user, pass)
    ))
}

@MainActor
public final class OnboardingViewModel: ObservableObject {
    public enum Phase: Equatable {
        case credentials
        case tenants([TenantSummary])
        case minting
        case done
    }

    @Published public var gatewayURL: String = ""
    @Published public var adminUsername: String = ""
    @Published public var adminPassword: String = ""
    @Published public var selectedTenantSlug: String? = nil
    @Published public private(set) var phase: Phase = .credentials
    @Published public private(set) var lastError: String? = nil
    @Published public private(set) var isWorking: Bool = false

    private let authStore: AuthStore
    private let factory: OnboardingClientFactory
    private let onComplete: @MainActor (StoredCredentials) -> Void

    public init(
        authStore: AuthStore,
        factory: @escaping OnboardingClientFactory = LiveOnboardingFactory,
        onComplete: @escaping @MainActor (StoredCredentials) -> Void
    ) {
        self.authStore = authStore
        self.factory = factory
        self.onComplete = onComplete
    }

    public var credentialsAreValid: Bool {
        guard let url = URL(string: gatewayURL.trimmingCharacters(in: .whitespaces)),
              url.scheme == "http" || url.scheme == "https"
        else { return false }
        return !adminUsername.isEmpty && !adminPassword.isEmpty
    }

    /// Phase 1 → 2 transition. Logs in (Basic), pulls tenants. If
    /// only one tenant comes back we auto-pick + mint immediately.
    public func submitCredentials() async {
        guard !isWorking else { return }
        guard let url = URL(string: gatewayURL.trimmingCharacters(in: .whitespaces)) else {
            lastError = "Gateway URL is malformed."
            return
        }
        isWorking = true
        defer { isWorking = false }
        lastError = nil

        let client = factory(url, adminUsername, adminPassword)
        do {
            try await client.login()
            let tenants = try await client.listTenants(forUser: adminUsername)
            // Auto-pick singleton tenant; otherwise let the operator
            // choose. Empty list is treated as a single-tenant
            // deployment with `nil` slug.
            if tenants.count <= 1 {
                let slug = tenants.first?.slug
                self.selectedTenantSlug = slug
                await mint(client: client, baseURL: url, tenantSlug: slug)
            } else {
                self.selectedTenantSlug = tenants.first?.slug
                self.phase = .tenants(tenants)
            }
        } catch {
            self.lastError = humanise(error)
            self.phase = .credentials
        }
    }

    /// Phase 2 → 3 transition. Mints the api_key for the selected
    /// tenant.
    public func confirmTenant() async {
        guard !isWorking else { return }
        guard let url = URL(string: gatewayURL.trimmingCharacters(in: .whitespaces)) else {
            lastError = "Gateway URL is malformed."
            phase = .credentials
            return
        }
        let client = factory(url, adminUsername, adminPassword)
        await mint(client: client, baseURL: url, tenantSlug: selectedTenantSlug)
    }

    private func mint(
        client: OnboardingClient,
        baseURL: URL,
        tenantSlug: String?
    ) async {
        isWorking = true
        defer { isWorking = false }
        phase = .minting
        do {
            let minted = try await client.mintApiKey(
                scope: "chat",
                username: adminUsername,
                label: "swift-mac onboarding"
            )
            try authStore.saveOnboarding(
                gatewayBaseURL: baseURL,
                adminUsername: adminUsername,
                adminPassword: adminPassword,
                tenantSlug: tenantSlug
            )
            try authStore.setApiKey(minted.token)
            phase = .done
            let creds = StoredCredentials(
                gatewayBaseURL: baseURL,
                adminUsername: adminUsername,
                adminPassword: adminPassword,
                apiKey: minted.token,
                tenantSlug: tenantSlug
            )
            onComplete(creds)
        } catch {
            self.lastError = humanise(error)
            self.phase = .credentials
        }
    }

    private func humanise(_ error: Error) -> String {
        switch error {
        case let GatewayClientError.http(status, body):
            if let body = body, !body.isEmpty {
                return "HTTP \(status): \(body.prefix(160))"
            }
            return "HTTP \(status)"
        case let GatewayClientError.transport(underlying):
            return "Network error: \(underlying.localizedDescription)"
        case let GatewayClientError.decoding(underlying):
            return "Server response could not be decoded: \(underlying)"
        case GatewayClientError.invalidURL(let path):
            return "Invalid URL for \(path)"
        case AuthStoreError.permissionDenied(let s):
            return "Keychain refused write (\(s)). Run from a signed binary or set the in-memory backend."
        default:
            return "\(error)"
        }
    }
}
