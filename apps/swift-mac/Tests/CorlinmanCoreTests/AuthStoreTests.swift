// Phase 4 W3 C4 iter 7 — `AuthStore` unit tests.
//
// These tests run against `InMemoryKeychain` so they don't depend on
// the host's signing identity. The mandated rows from the design
// matrix (`docs/design/phase4-w3-c4-design.md:357-360`):
//
//   - auth_store_keychain_round_trip
//   - auth_store_first_launch_onboarding
//
// Plus three coverage tests for tenant preference + clear-all
// + apiKey-rotation behaviours we'd otherwise be flying blind on.

import XCTest

@testable import CorlinmanCore

final class AuthStoreTests: XCTestCase {

    private func make() -> (AuthStore, InMemoryKeychain, EphemeralTenantPreference) {
        let kc = InMemoryKeychain()
        let prefs = EphemeralTenantPreference()
        return (AuthStore(keychain: kc, tenants: prefs), kc, prefs)
    }

    // MARK: - Mandated: keychain round-trip

    func test_authStore_keychainRoundTrip() throws {
        let (store, _, _) = make()

        // Save the full credential triple via the public surface,
        // then read individual pieces back.
        try store.saveOnboarding(
            gatewayBaseURL: URL(string: "https://gateway.example.com")!,
            adminUsername: "admin",
            adminPassword: "hunter2",
            tenantSlug: "acme"
        )
        try store.setApiKey("ck_test_token_xyz")

        let creds = store.credentials
        XCTAssertNotNil(creds)
        XCTAssertEqual(creds?.gatewayBaseURL.absoluteString, "https://gateway.example.com")
        XCTAssertEqual(creds?.adminUsername, "admin")
        XCTAssertEqual(creds?.adminPassword, "hunter2")
        XCTAssertEqual(creds?.tenantSlug, "acme")
        XCTAssertEqual(creds?.apiKey, "ck_test_token_xyz")
        XCTAssertEqual(store.apiKey, "ck_test_token_xyz")
        XCTAssertEqual(store.tenantSlug, "acme")
    }

    // MARK: - Mandated: first-launch onboarding detection

    func test_authStore_firstLaunchOnboarding() throws {
        let (store, _, _) = make()
        XCTAssertTrue(store.requiresOnboarding,
                      "fresh store with no keychain entries must require onboarding")

        try store.saveOnboarding(
            gatewayBaseURL: URL(string: "https://g.example")!,
            adminUsername: "u",
            adminPassword: "p",
            tenantSlug: nil
        )
        XCTAssertFalse(store.requiresOnboarding,
                       "after saveOnboarding the keychain has all three rows")
    }

    // MARK: - Coverage: tenant preference is non-secret + persists separately

    func test_authStore_tenantSlugIsBackedByPreferenceStore() {
        let (store, _, prefs) = make()
        store.tenantSlug = "tenant-7"
        XCTAssertEqual(prefs.currentTenantSlug(), "tenant-7")
        XCTAssertEqual(store.tenantSlug, "tenant-7")
        store.tenantSlug = nil
        XCTAssertNil(prefs.currentTenantSlug())
    }

    // MARK: - Coverage: clearAll wipes every keychain + preference row

    func test_authStore_clearAllRemovesEverything() throws {
        let (store, _, _) = make()
        try store.saveOnboarding(
            gatewayBaseURL: URL(string: "https://g")!,
            adminUsername: "u",
            adminPassword: "p",
            tenantSlug: "t"
        )
        try store.setApiKey("ck_xxx")
        XCTAssertNotNil(store.credentials)

        try store.clearAll()
        XCTAssertNil(store.credentials)
        XCTAssertNil(store.apiKey)
        XCTAssertNil(store.tenantSlug)
        XCTAssertTrue(store.requiresOnboarding)
    }

    // MARK: - Coverage: api_key rotation overwrites in place

    func test_authStore_setApiKeyOverwritesExisting() throws {
        let (store, _, _) = make()
        try store.setApiKey("ck_one")
        try store.setApiKey("ck_two")
        XCTAssertEqual(store.apiKey, "ck_two")
    }

    // MARK: - InMemoryKeychain semantics — sanity check

    func test_inMemoryKeychain_notFoundThrows() {
        let kc = InMemoryKeychain()
        XCTAssertThrowsError(try kc.read(service: "svc", account: "acc")) { err in
            guard case AuthStoreError.notFound = err else {
                XCTFail("expected .notFound, got \(err)")
                return
            }
        }
    }
}
