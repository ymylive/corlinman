// Phase 4 W3 C4 iter 7 — `OnboardingViewModel` unit tests.
//
// We drive the view model with a `FakeOnboardingClient` so the
// tests stay deterministic and don't require a running gateway.
// Three flows we want to lock in:
//
//   - happy path with single tenant → auto-mint → .done
//   - happy path with multi tenant → .tenants phase → confirm → mint
//   - mint failure → .credentials with `lastError`
//
// `swift test` is unrunnable in the sandbox without an SDK; these
// tests run on the macOS CI runner (iter 9).

import XCTest

@testable import CorlinmanCore
@testable import CorlinmanUI

@MainActor
final class OnboardingViewModelTests: XCTestCase {

    private final class FakeClient: OnboardingClient, @unchecked Sendable {
        var loginCalls = 0
        var listCalls = 0
        var mintCalls = 0
        var loginError: Error?
        var tenants: [TenantSummary] = []
        var mintResult: Result<MintedApiKey, Error> = .failure(GatewayClientError.invalidURL("not configured"))

        func login() async throws {
            loginCalls += 1
            if let err = loginError { throw err }
        }

        func listTenants(forUser username: String) async throws -> [TenantSummary] {
            listCalls += 1
            return tenants
        }

        func mintApiKey(scope: String, username: String?, label: String?) async throws -> MintedApiKey {
            mintCalls += 1
            switch mintResult {
            case .success(let m): return m
            case .failure(let e): throw e
            }
        }
    }

    private func sampleMint(token: String = "ck_token_xyz") -> MintedApiKey {
        // Decode through JSONDecoder to exercise the same path the
        // real client takes — ensures field names stay in sync if
        // the wire shape evolves.
        let json = """
        {
          "key_id": "key-1",
          "tenant_id": "tenant-1",
          "username": "admin",
          "scope": "chat",
          "label": "swift-mac onboarding",
          "token": "\(token)",
          "created_at_ms": 1700000000000
        }
        """
        return try! JSONDecoder().decode(MintedApiKey.self, from: Data(json.utf8))
    }

    private func makeAuthStore() -> AuthStore {
        AuthStore(keychain: InMemoryKeychain(), tenants: EphemeralTenantPreference())
    }

    // MARK: - Single-tenant auto-mint

    func test_submitCredentials_singleTenantAutoMints() async throws {
        let fake = FakeClient()
        fake.tenants = [TenantSummary(id: "t1", slug: "solo", display_name: "Solo")]
        fake.mintResult = .success(sampleMint())

        let auth = makeAuthStore()
        var completedWith: StoredCredentials?
        let vm = OnboardingViewModel(
            authStore: auth,
            factory: { _, _, _ in fake },
            onComplete: { completedWith = $0 }
        )
        vm.gatewayURL = "https://gateway.example.com"
        vm.adminUsername = "admin"
        vm.adminPassword = "hunter2"
        XCTAssertTrue(vm.credentialsAreValid)

        await vm.submitCredentials()

        XCTAssertEqual(fake.loginCalls, 1)
        XCTAssertEqual(fake.listCalls, 1)
        XCTAssertEqual(fake.mintCalls, 1, "single tenant should auto-mint without going through .tenants phase")
        XCTAssertEqual(vm.phase, .done)
        XCTAssertEqual(auth.apiKey, "ck_token_xyz")
        XCTAssertEqual(auth.tenantSlug, "solo")
        XCTAssertEqual(completedWith?.tenantSlug, "solo")
        XCTAssertNil(vm.lastError)
    }

    // MARK: - Multi-tenant flow goes through `.tenants` phase

    func test_submitCredentials_multiTenantAdvancesToTenantsPhase() async throws {
        let fake = FakeClient()
        fake.tenants = [
            TenantSummary(id: "t1", slug: "alpha", display_name: "Alpha"),
            TenantSummary(id: "t2", slug: "beta", display_name: "Beta"),
        ]
        let auth = makeAuthStore()
        let vm = OnboardingViewModel(
            authStore: auth,
            factory: { _, _, _ in fake },
            onComplete: { _ in }
        )
        vm.gatewayURL = "https://g"
        vm.adminUsername = "u"
        vm.adminPassword = "p"

        await vm.submitCredentials()

        guard case let .tenants(list) = vm.phase else {
            XCTFail("expected .tenants phase, got \(vm.phase)")
            return
        }
        XCTAssertEqual(list.count, 2)
        XCTAssertEqual(vm.selectedTenantSlug, "alpha", "first tenant pre-selected")
        XCTAssertEqual(fake.mintCalls, 0, "should not have minted yet")

        // Simulate the operator picking beta.
        vm.selectedTenantSlug = "beta"
        fake.mintResult = .success(sampleMint(token: "ck_beta_token"))
        await vm.confirmTenant()

        XCTAssertEqual(fake.mintCalls, 1)
        XCTAssertEqual(vm.phase, .done)
        XCTAssertEqual(auth.apiKey, "ck_beta_token")
        XCTAssertEqual(auth.tenantSlug, "beta")
    }

    // MARK: - Mint failure routes back to `.credentials`

    func test_submitCredentials_mintFailureSurfacesError() async throws {
        let fake = FakeClient()
        fake.tenants = [TenantSummary(id: "t1", slug: "solo", display_name: nil)]
        fake.mintResult = .failure(GatewayClientError.http(status: 503, body: "tenants_disabled"))

        let auth = makeAuthStore()
        let vm = OnboardingViewModel(
            authStore: auth,
            factory: { _, _, _ in fake },
            onComplete: { _ in XCTFail("should not complete on mint failure") }
        )
        vm.gatewayURL = "https://g"
        vm.adminUsername = "u"
        vm.adminPassword = "p"

        await vm.submitCredentials()

        XCTAssertEqual(vm.phase, .credentials)
        XCTAssertNotNil(vm.lastError)
        XCTAssertNil(auth.apiKey, "no api_key should be stored on failure")
        XCTAssertTrue(auth.requiresOnboarding,
                      "credentials should not be considered onboarded after mint fails")
    }

    // MARK: - credentialsAreValid gates submission

    func test_credentialsAreValid_requiresHttpScheme() {
        let vm = OnboardingViewModel(
            authStore: makeAuthStore(),
            factory: { _, _, _ in FakeClient() },
            onComplete: { _ in }
        )
        vm.gatewayURL = "ftp://wat"
        vm.adminUsername = "u"
        vm.adminPassword = "p"
        XCTAssertFalse(vm.credentialsAreValid)

        vm.gatewayURL = "https://g.example"
        XCTAssertTrue(vm.credentialsAreValid)

        vm.adminPassword = ""
        XCTAssertFalse(vm.credentialsAreValid)
    }
}
