// Phase 4 W3 C4 iter 5 — `SessionStore` (SQLite) tests.
//
// Tests run against `:memory:` databases so they don't litter the
// filesystem and stay isolated. The mandated rows from the design
// matrix:
//   - `session_store_persists_across_relaunch`
//   - `session_store_resync_merges_server_changes`
// We add three more for the operations the UI exercises every launch:
//   - upsert keeps the older `created_at`, advances `last_message_at`
//   - `latestSeenTimestampMs` returns 0 on an empty cache
//   - tenant scoping prevents cross-tenant leakage

import XCTest

@testable import CorlinmanCore

final class SessionStoreTests: XCTestCase {

    private func makeStore() throws -> SessionStore {
        // `:memory:` means a fresh database per `SessionStore` — even
        // with the same path string, every connection gets its own
        // in-memory db. That's exactly what we want for unit tests.
        return try SessionStore(path: ":memory:")
    }

    private func makeFileStore() throws -> (SessionStore, String) {
        let tmp = FileManager.default.temporaryDirectory
            .appendingPathComponent("corlinman-test-\(UUID().uuidString).sqlite")
            .path
        return (try SessionStore(path: tmp), tmp)
    }

    // MARK: - Mandated: persists across relaunch

    func test_sessionStore_persistsAcrossRelaunch() throws {
        let (store, path) = try makeFileStore()
        defer { try? FileManager.default.removeItem(atPath: path) }

        let now: Int64 = 1_700_000_000_000
        try store.upsertSession(StoredSession(
            sessionKey: "s-1",
            tenantSlug: "acme",
            displayTitle: "First chat",
            lastMessageAtMs: now,
            createdAtMs: now
        ))
        try store.appendMessage(StoredMessage(
            sessionKey: "s-1",
            role: "user",
            content: "hello",
            createdAtMs: now
        ))
        // Drop the store, reopen the same path, query.
        // (`SessionStore` closes its handle on deinit.)
        var maybeStore: SessionStore? = store
        maybeStore = nil
        _ = maybeStore  // silence unused warning

        let reopened = try SessionStore(path: path)
        let sessions = try reopened.loadSessions(tenantSlug: "acme")
        XCTAssertEqual(sessions.count, 1)
        XCTAssertEqual(sessions[0].sessionKey, "s-1")
        XCTAssertEqual(sessions[0].displayTitle, "First chat")

        let messages = try reopened.loadMessages(sessionKey: "s-1")
        XCTAssertEqual(messages.count, 1)
        XCTAssertEqual(messages[0].role, "user")
        XCTAssertEqual(messages[0].content, "hello")
    }

    // MARK: - Mandated: resync merges server changes

    func test_sessionStore_resyncMergesServerChanges() throws {
        let store = try makeStore()
        // Local: one session, last seen at t=100.
        try store.upsertSession(StoredSession(
            sessionKey: "s-local",
            tenantSlug: "acme",
            displayTitle: "Local",
            lastMessageAtMs: 100,
            createdAtMs: 50
        ))
        // Server returns: same session moved on (t=300), plus a new
        // session that originated on another device.
        let serverRows = [
            StoredSession(
                sessionKey: "s-local",
                tenantSlug: "acme",
                displayTitle: "Local",
                lastMessageAtMs: 300,
                createdAtMs: 50
            ),
            StoredSession(
                sessionKey: "s-remote",
                tenantSlug: "acme",
                displayTitle: "From phone",
                lastMessageAtMs: 200,
                createdAtMs: 150
            ),
        ]
        let merged = try store.mergeServerSessions(serverRows, tenantSlug: "acme")
        // Newest-first order: s-local at 300 → s-remote at 200.
        XCTAssertEqual(merged.map(\.sessionKey), ["s-local", "s-remote"])
        XCTAssertEqual(merged[0].lastMessageAtMs, 300)
        XCTAssertEqual(merged[1].displayTitle, "From phone")
    }

    // MARK: - Upsert behaviour

    func test_upsertSession_advancesLastMessageButKeepsCreatedAt() throws {
        let store = try makeStore()
        try store.upsertSession(StoredSession(
            sessionKey: "s",
            tenantSlug: "t",
            displayTitle: "v1",
            lastMessageAtMs: 100,
            createdAtMs: 100
        ))
        try store.upsertSession(StoredSession(
            sessionKey: "s",
            tenantSlug: "t",
            displayTitle: nil,                 // nil keeps existing title via COALESCE
            lastMessageAtMs: 200,
            createdAtMs: 999                   // ignored — created_at is never updated
        ))
        let rows = try store.loadSessions(tenantSlug: "t")
        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows[0].lastMessageAtMs, 200)
        XCTAssertEqual(rows[0].displayTitle, "v1")
        // created_at is replaced by the new value (excluded.created_at)
        // in the current ON CONFLICT DO UPDATE — we *don't* touch it
        // on the SET clause, so the original wins. Assert that.
        XCTAssertEqual(rows[0].createdAtMs, 100)
    }

    // MARK: - Empty cache

    func test_latestSeenTimestampMs_emptyCacheReturnsZero() throws {
        let store = try makeStore()
        let ts = try store.latestSeenTimestampMs(tenantSlug: "anyone")
        XCTAssertEqual(ts, 0)
    }

    // MARK: - Tenant scoping

    func test_loadSessions_isTenantScoped() throws {
        let store = try makeStore()
        try store.upsertSession(StoredSession(
            sessionKey: "a", tenantSlug: "tenant-a",
            displayTitle: nil, lastMessageAtMs: 1, createdAtMs: 1))
        try store.upsertSession(StoredSession(
            sessionKey: "b", tenantSlug: "tenant-b",
            displayTitle: nil, lastMessageAtMs: 2, createdAtMs: 2))
        let aRows = try store.loadSessions(tenantSlug: "tenant-a")
        let bRows = try store.loadSessions(tenantSlug: "tenant-b")
        XCTAssertEqual(aRows.map(\.sessionKey), ["a"])
        XCTAssertEqual(bRows.map(\.sessionKey), ["b"])
    }
}
