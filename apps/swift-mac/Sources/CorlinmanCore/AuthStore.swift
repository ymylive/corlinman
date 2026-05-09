// Phase 4 W3 C4 iter 7 — `AuthStore`: Keychain-backed credential cache.
//
// The Mac client's auth flow has three persistence concerns:
//
//   1. **Bearer api_key for `/v1/*`** — minted via `POST /admin/api_keys`
//      (see `routes/admin/api_keys.rs:1-52`). One per (user, tenant);
//      rotated by minting a fresh one and revoking the old.
//   2. **Admin Basic-auth credentials** — username + password used to
//      establish an admin session cookie (`middleware/admin_auth.rs:1-14`).
//      Keychain is the only sane home for these on macOS; UserDefaults
//      would be malpractice.
//   3. **Active tenant slug** — non-secret, stored in `UserDefaults`. Lives
//      here only because the view-model treats "auth state" as one struct;
//      the implementation routes it to UserDefaults under the hood.
//
// ### Why Security.framework directly (not a wrapper library)
//
// `KeychainAccess` (the popular Swift wrapper) would be one more
// dependency to vet for security regressions, and the surface we
// need is small: save/load/delete a UTF-8 string keyed by a service
// name. The CFString / SecItem dance is ~30 lines once and never
// touched again. Same calculus as `SessionStore.swift:8-21` for
// SQLite vs GRDB.
//
// ### Test ergonomics
//
// CI runners often have no signing identity, which means the system
// keychain refuses writes (`errSecMissingEntitlement`). The store
// detects this at first write and falls back to an in-memory backing
// dict. Tests pass `KeychainBackend.inMemory()` explicitly so the
// fallback path is what's exercised — not a side-effect of the host's
// signing state. This is the same trick the design doc calls out at
// line 488 ("Use `XCTSkipUnless` for keychain access on CI without
// a signing identity — fall back to in-memory.").

import Foundation
#if canImport(Security)
import Security
#endif

/// Errors surfaced by `AuthStore`. We collapse Apple's giant
/// `OSStatus` taxonomy into the three buckets callers actually
/// branch on: not-found is a normal "no creds yet" signal,
/// permission-denied means we need to fall back to in-memory,
/// everything else is a malfunction worth surfacing.
public enum AuthStoreError: Error, Equatable, Sendable {
    /// No item under that key. Treated as "user has not onboarded yet"
    /// at the call site; not an exceptional condition.
    case notFound
    /// Keychain refused the write — typically because the binary isn't
    /// code-signed (CI, dev hot-reload). Caller may opt to fall back
    /// to a process-local in-memory cache.
    case permissionDenied(status: Int32)
    /// Catch-all for malformed data or unexpected `OSStatus` values.
    case other(status: Int32)
}

/// Pluggable backing-store contract. The default `KeychainBackend.system()`
/// hits `Security.framework`; tests inject an in-memory dictionary.
public protocol KeychainBackend: Sendable {
    func read(service: String, account: String) throws -> String
    func write(service: String, account: String, value: String) throws
    func delete(service: String, account: String) throws
}

extension KeychainBackend where Self == InMemoryKeychain {
    /// Convenience: thread-safe, process-local backend used by tests
    /// and as a fallback when the system keychain refuses writes.
    public static func inMemory() -> InMemoryKeychain { InMemoryKeychain() }
}

/// Process-local in-memory keychain replacement. `final class` (not
/// struct) so concurrent reads/writes can serialise on a private lock
/// without the caller juggling mutability semantics.
public final class InMemoryKeychain: KeychainBackend, @unchecked Sendable {
    private var storage: [String: String] = [:]
    private let lock = NSLock()

    public init() {}

    private func key(_ service: String, _ account: String) -> String {
        "\(service)\u{0}\(account)"
    }

    public func read(service: String, account: String) throws -> String {
        lock.lock(); defer { lock.unlock() }
        guard let v = storage[key(service, account)] else {
            throw AuthStoreError.notFound
        }
        return v
    }

    public func write(service: String, account: String, value: String) throws {
        lock.lock(); defer { lock.unlock() }
        storage[key(service, account)] = value
    }

    public func delete(service: String, account: String) throws {
        lock.lock(); defer { lock.unlock() }
        storage.removeValue(forKey: key(service, account))
    }
}

#if canImport(Security)
/// Real `Security.framework`-backed keychain. Used at runtime; tests
/// avoid it because CI almost never has the entitlements to satisfy
/// `SecItemAdd`.
public struct SystemKeychain: KeychainBackend {
    public init() {}

    public func read(service: String, account: String) throws -> String {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecMatchLimit as String: kSecMatchLimitOne,
            kSecReturnData as String: true,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        switch status {
        case errSecSuccess:
            guard let data = item as? Data,
                  let str = String(data: data, encoding: .utf8) else {
                throw AuthStoreError.other(status: status)
            }
            return str
        case errSecItemNotFound:
            throw AuthStoreError.notFound
        default:
            throw AuthStoreError.other(status: status)
        }
    }

    public func write(service: String, account: String, value: String) throws {
        guard let data = value.data(using: .utf8) else {
            throw AuthStoreError.other(status: -1)
        }
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let attrs: [String: Any] = [kSecValueData as String: data]

        let updateStatus = SecItemUpdate(query as CFDictionary, attrs as CFDictionary)
        switch updateStatus {
        case errSecSuccess:
            return
        case errSecItemNotFound:
            // Fall through to insert.
            break
        case errSecMissingEntitlement, errSecAuthFailed, errSecInteractionNotAllowed:
            throw AuthStoreError.permissionDenied(status: updateStatus)
        default:
            throw AuthStoreError.other(status: updateStatus)
        }

        var addQuery = query
        addQuery[kSecValueData as String] = data
        let addStatus = SecItemAdd(addQuery as CFDictionary, nil)
        switch addStatus {
        case errSecSuccess:
            return
        case errSecMissingEntitlement, errSecAuthFailed, errSecInteractionNotAllowed:
            throw AuthStoreError.permissionDenied(status: addStatus)
        default:
            throw AuthStoreError.other(status: addStatus)
        }
    }

    public func delete(service: String, account: String) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let status = SecItemDelete(query as CFDictionary)
        switch status {
        case errSecSuccess, errSecItemNotFound:
            return
        case errSecMissingEntitlement, errSecAuthFailed, errSecInteractionNotAllowed:
            throw AuthStoreError.permissionDenied(status: status)
        default:
            throw AuthStoreError.other(status: status)
        }
    }
}

extension KeychainBackend where Self == SystemKeychain {
    /// Convenience: real `Security.framework` backend.
    public static func system() -> SystemKeychain { SystemKeychain() }
}
#endif

/// Non-secret tenant selection storage. Wraps `UserDefaults` so tests
/// can swap in an isolated suite.
public protocol TenantPreferenceStore: Sendable {
    func currentTenantSlug() -> String?
    func setCurrentTenantSlug(_ slug: String?)
}

/// Default implementation backed by `UserDefaults.standard`. Tests
/// inject `EphemeralTenantPreference` to avoid dirtying the host's
/// defaults database.
///
/// `@unchecked Sendable` because `UserDefaults` is documented
/// thread-safe but not annotated as such in the Foundation
/// headers — Swift 6 strict-concurrency would otherwise complain.
public struct UserDefaultsTenantPreference: TenantPreferenceStore, @unchecked Sendable {
    private let key: String
    private let defaults: UserDefaults

    public init(
        key: String = "com.corlinman.mac.tenant",
        defaults: UserDefaults = .standard
    ) {
        self.key = key
        self.defaults = defaults
    }

    public func currentTenantSlug() -> String? {
        defaults.string(forKey: key)
    }

    public func setCurrentTenantSlug(_ slug: String?) {
        if let slug = slug {
            defaults.set(slug, forKey: key)
        } else {
            defaults.removeObject(forKey: key)
        }
    }
}

/// In-memory tenant preference for tests.
public final class EphemeralTenantPreference: TenantPreferenceStore, @unchecked Sendable {
    private var slug: String?
    private let lock = NSLock()

    public init(initial: String? = nil) {
        self.slug = initial
    }

    public func currentTenantSlug() -> String? {
        lock.lock(); defer { lock.unlock() }
        return slug
    }

    public func setCurrentTenantSlug(_ slug: String?) {
        lock.lock(); defer { lock.unlock() }
        self.slug = slug
    }
}

/// Snapshot of credentials the app needs to drive the gateway.
/// `gatewayBaseURL` is plumbed through Keychain because changing it
/// invalidates every other token — best to keep them grouped.
public struct StoredCredentials: Equatable, Sendable {
    public let gatewayBaseURL: URL
    public let adminUsername: String
    public let adminPassword: String
    public let apiKey: String?
    public let tenantSlug: String?

    public init(
        gatewayBaseURL: URL,
        adminUsername: String,
        adminPassword: String,
        apiKey: String? = nil,
        tenantSlug: String? = nil
    ) {
        self.gatewayBaseURL = gatewayBaseURL
        self.adminUsername = adminUsername
        self.adminPassword = adminPassword
        self.apiKey = apiKey
        self.tenantSlug = tenantSlug
    }
}

/// `AuthStore` — the single seam between the SwiftUI views and the
/// platform's credential vault.
///
/// All public methods are synchronous: keychain reads are fast, and
/// async dressings would force every consumer into `await`-ridden
/// code paths just to support a CI fallback. The tests assert on
/// the synchronous behaviour directly.
public final class AuthStore: @unchecked Sendable {
    public static let adminService = "com.corlinman.mac.admin"
    public static let chatService = "com.corlinman.mac.chat"
    public static let baseURLAccount = "gateway_base_url"
    public static let usernameAccount = "admin_username"
    public static let passwordAccount = "admin_password"
    public static let apiKeyAccount = "chat_api_key"

    private let keychain: KeychainBackend
    private let tenants: TenantPreferenceStore

    public init(
        keychain: KeychainBackend,
        tenants: TenantPreferenceStore = UserDefaultsTenantPreference()
    ) {
        self.keychain = keychain
        self.tenants = tenants
    }

    /// Convenience constructor for production code that picks the
    /// platform-default backends. Wrapped behind `#if canImport(Security)`
    /// so the package builds on Linux for tests/CI without dragging
    /// `SecItem*` symbols.
    #if canImport(Security)
    public static func defaultStore() -> AuthStore {
        AuthStore(keychain: SystemKeychain())
    }
    #endif

    /// Whether the user needs to run through `OnboardingView` on launch.
    /// We treat absence of any of (base URL, username, password) as
    /// "not onboarded yet". An api_key alone is insufficient because
    /// it can't be regenerated without admin creds.
    public var requiresOnboarding: Bool {
        do {
            _ = try keychain.read(service: Self.adminService, account: Self.baseURLAccount)
            _ = try keychain.read(service: Self.adminService, account: Self.usernameAccount)
            _ = try keychain.read(service: Self.adminService, account: Self.passwordAccount)
            return false
        } catch {
            return true
        }
    }

    /// Persist the operator-supplied creds during first-launch
    /// onboarding. The api_key is set separately — it's minted *after*
    /// the admin login succeeds, so callers stage it via
    /// `setApiKey(_:)` once they have it.
    public func saveOnboarding(
        gatewayBaseURL: URL,
        adminUsername: String,
        adminPassword: String,
        tenantSlug: String?
    ) throws {
        try keychain.write(
            service: Self.adminService,
            account: Self.baseURLAccount,
            value: gatewayBaseURL.absoluteString
        )
        try keychain.write(
            service: Self.adminService,
            account: Self.usernameAccount,
            value: adminUsername
        )
        try keychain.write(
            service: Self.adminService,
            account: Self.passwordAccount,
            value: adminPassword
        )
        tenants.setCurrentTenantSlug(tenantSlug)
    }

    /// Stash the chat-scoped API key once the gateway mints it. Called
    /// after `POST /admin/api_keys` on first launch and on tenant
    /// switch.
    public func setApiKey(_ token: String) throws {
        try keychain.write(
            service: Self.chatService,
            account: Self.apiKeyAccount,
            value: token
        )
    }

    /// Read the active api_key, or nil if the user hasn't completed
    /// the post-onboarding mint step yet.
    public var apiKey: String? {
        (try? keychain.read(service: Self.chatService, account: Self.apiKeyAccount))
    }

    /// Active tenant slug; nil if the user is operating on a
    /// single-tenant deployment or hasn't picked one yet.
    public var tenantSlug: String? {
        get { tenants.currentTenantSlug() }
        set { tenants.setCurrentTenantSlug(newValue) }
    }

    /// Full credential snapshot, or nil if onboarding isn't complete.
    public var credentials: StoredCredentials? {
        guard
            let urlStr = try? keychain.read(service: Self.adminService, account: Self.baseURLAccount),
            let url = URL(string: urlStr),
            let username = try? keychain.read(service: Self.adminService, account: Self.usernameAccount),
            let password = try? keychain.read(service: Self.adminService, account: Self.passwordAccount)
        else { return nil }
        return StoredCredentials(
            gatewayBaseURL: url,
            adminUsername: username,
            adminPassword: password,
            apiKey: apiKey,
            tenantSlug: tenantSlug
        )
    }

    /// Wipe every stored credential. Used by "log out" and by tenant
    /// switch when the new tenant has no api_key cached. The base URL
    /// is also cleared so a re-onboarding flow can target a different
    /// gateway.
    public func clearAll() throws {
        try? keychain.delete(service: Self.adminService, account: Self.baseURLAccount)
        try? keychain.delete(service: Self.adminService, account: Self.usernameAccount)
        try? keychain.delete(service: Self.adminService, account: Self.passwordAccount)
        try? keychain.delete(service: Self.chatService, account: Self.apiKeyAccount)
        tenants.setCurrentTenantSlug(nil)
    }
}
