// Phase 4 W3 C4 iter 5 — local session persistence (SQLite via `SQLite3`).
//
// Why direct `SQLite3` and not GRDB-Swift (the design doc's stated
// pick at line 314)? Two reasons:
//
//   1. SwiftPM dependency-graph hygiene. GRDB pulls a hefty external
//      package; pinning + resolver work isn't free, and the C4 design
//      explicitly notes (line 41 of `Package.swift`) that iter 1
//      stays dep-free. Adding GRDB now means a new resolver-online
//      moment for every contributor, vs. zero new deps for SQLite3
//      which ships in every macOS / iOS SDK as a system module.
//   2. The schema we need — two tables, six columns, one index —
//      doesn't exercise a tenth of GRDB's surface. The 200-line
//      direct binding here is auditable and stays small enough that
//      a future migration to GRDB is mechanical.
//
// If/when query complexity grows — joins across more tables, observed
// FetchedResults, migrations beyond the linear v0→v1 we ship today —
// swap in GRDB and delete this file. The public API
// (`SessionStore.persist(_:)`, `loadSessions()`, etc.) is the contract;
// the storage layer is implementation detail.
//
// Schema mirrors `docs/design/phase4-w3-c4-design.md:316-333` verbatim
// so the design-doc → code mapping stays one-to-one.

import Foundation
import SQLite3

// `SQLITE_TRANSIENT` lives in the C header as a function-style macro
// that Swift can't bridge directly. The conventional shim:
private let SQLITE_TRANSIENT_BRIDGE = unsafeBitCast(
    -1,
    to: sqlite3_destructor_type.self
)

/// Public model for one row in `sessions`.
public struct StoredSession: Equatable, Sendable {
    public let sessionKey: String
    public let tenantSlug: String
    public let displayTitle: String?
    public let lastMessageAtMs: Int64
    public let createdAtMs: Int64

    public init(
        sessionKey: String,
        tenantSlug: String,
        displayTitle: String?,
        lastMessageAtMs: Int64,
        createdAtMs: Int64
    ) {
        self.sessionKey = sessionKey
        self.tenantSlug = tenantSlug
        self.displayTitle = displayTitle
        self.lastMessageAtMs = lastMessageAtMs
        self.createdAtMs = createdAtMs
    }
}

/// Public model for one row in `messages`.
public struct StoredMessage: Equatable, Sendable {
    public let id: Int64?       // nil for inserts; assigned post-write
    public let sessionKey: String
    public let role: String     // "user" | "assistant" | "tool"
    public let content: String
    public let toolCallId: String?
    public let createdAtMs: Int64

    public init(
        id: Int64? = nil,
        sessionKey: String,
        role: String,
        content: String,
        toolCallId: String? = nil,
        createdAtMs: Int64
    ) {
        self.id = id
        self.sessionKey = sessionKey
        self.role = role
        self.content = content
        self.toolCallId = toolCallId
        self.createdAtMs = createdAtMs
    }
}

/// Errors surfaced by `SessionStore`. Distinct from raw SQLite return
/// codes so callers can pattern-match without leaking the C-level
/// errno into the UI layer.
public enum SessionStoreError: Error, Sendable {
    case open(path: String, code: Int32, message: String)
    case prepare(sql: String, code: Int32, message: String)
    case step(sql: String, code: Int32, message: String)
}

/// Thread-safe wrapper around the local sessions database. Public
/// methods serialise on a private dispatch queue so concurrent
/// `persist` + `loadSessions` calls from the UI don't race.
public final class SessionStore: @unchecked Sendable {
    private var db: OpaquePointer?
    private let queue = DispatchQueue(label: "com.corlinman.mac.session-store")
    private let path: String

    /// Open or create the database at `path`. The default location
    /// matches the design doc:
    /// `~/Library/Application Support/Corlinman/sessions.sqlite`.
    /// Tests pass `:memory:` for fast, isolated runs.
    public init(path: String) throws {
        self.path = path
        var handle: OpaquePointer?
        let rc = sqlite3_open_v2(
            path,
            &handle,
            SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX,
            nil
        )
        guard rc == SQLITE_OK, let h = handle else {
            let msg = handle.flatMap { String(cString: sqlite3_errmsg($0)) } ?? "open failed"
            if let h = handle { sqlite3_close(h) }
            throw SessionStoreError.open(path: path, code: rc, message: msg)
        }
        self.db = h
        try migrate()
    }

    deinit {
        if let db = db { sqlite3_close(db) }
    }

    /// Default on-disk path under Application Support. Creates the
    /// directory if missing. Throws if the filesystem refuses.
    public static func defaultPath() throws -> String {
        let appSupport = try FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let dir = appSupport.appendingPathComponent("Corlinman", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("sessions.sqlite").path
    }

    // MARK: - Schema

    private func migrate() throws {
        let createSessions = """
        CREATE TABLE IF NOT EXISTS sessions (
          session_key TEXT PRIMARY KEY,
          tenant_slug TEXT NOT NULL,
          display_title TEXT,
          last_message_at INTEGER NOT NULL,
          created_at INTEGER NOT NULL
        );
        """
        let createMessages = """
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_key TEXT NOT NULL REFERENCES sessions(session_key) ON DELETE CASCADE,
          role TEXT NOT NULL,
          content TEXT NOT NULL,
          tool_call_id TEXT,
          created_at INTEGER NOT NULL
        );
        """
        let createIndex = """
        CREATE INDEX IF NOT EXISTS idx_messages_session
          ON messages(session_key, created_at);
        """
        try exec(createSessions)
        try exec(createMessages)
        try exec(createIndex)
        // Foreign keys are off by default in sqlite3 — enable so the
        // ON DELETE CASCADE actually fires.
        try exec("PRAGMA foreign_keys = ON;")
    }

    // MARK: - Public surface

    /// Insert or replace a session row. `last_message_at` is the
    /// server-side timestamp when the merge comes from a resync; the
    /// caller picks a sensible value for local-only inserts.
    public func upsertSession(_ s: StoredSession) throws {
        try queue.sync {
            let sql = """
            INSERT INTO sessions (session_key, tenant_slug, display_title, last_message_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_key) DO UPDATE SET
              tenant_slug = excluded.tenant_slug,
              display_title = COALESCE(excluded.display_title, sessions.display_title),
              last_message_at = MAX(sessions.last_message_at, excluded.last_message_at);
            """
            try withStatement(sql) { stmt in
                sqlite3_bind_text(stmt, 1, s.sessionKey, -1, SQLITE_TRANSIENT_BRIDGE)
                sqlite3_bind_text(stmt, 2, s.tenantSlug, -1, SQLITE_TRANSIENT_BRIDGE)
                if let t = s.displayTitle {
                    sqlite3_bind_text(stmt, 3, t, -1, SQLITE_TRANSIENT_BRIDGE)
                } else {
                    sqlite3_bind_null(stmt, 3)
                }
                sqlite3_bind_int64(stmt, 4, s.lastMessageAtMs)
                sqlite3_bind_int64(stmt, 5, s.createdAtMs)
                let rc = sqlite3_step(stmt)
                guard rc == SQLITE_DONE else {
                    throw SessionStoreError.step(sql: sql, code: rc, message: lastErrMsg())
                }
            }
        }
    }

    /// Append a message row. The auto-increment id isn't reflected
    /// back into the input struct — call sites that need the assigned
    /// id can subsequently `loadMessages(sessionKey:)`.
    public func appendMessage(_ m: StoredMessage) throws {
        try queue.sync {
            let sql = """
            INSERT INTO messages (session_key, role, content, tool_call_id, created_at)
            VALUES (?, ?, ?, ?, ?);
            """
            try withStatement(sql) { stmt in
                sqlite3_bind_text(stmt, 1, m.sessionKey, -1, SQLITE_TRANSIENT_BRIDGE)
                sqlite3_bind_text(stmt, 2, m.role, -1, SQLITE_TRANSIENT_BRIDGE)
                sqlite3_bind_text(stmt, 3, m.content, -1, SQLITE_TRANSIENT_BRIDGE)
                if let t = m.toolCallId {
                    sqlite3_bind_text(stmt, 4, t, -1, SQLITE_TRANSIENT_BRIDGE)
                } else {
                    sqlite3_bind_null(stmt, 4)
                }
                sqlite3_bind_int64(stmt, 5, m.createdAtMs)
                let rc = sqlite3_step(stmt)
                guard rc == SQLITE_DONE else {
                    throw SessionStoreError.step(sql: sql, code: rc, message: lastErrMsg())
                }
            }
        }
    }

    /// Load all sessions for a tenant, newest-first by
    /// `last_message_at`. The view model uses this on launch to
    /// populate the sidebar before any network roundtrip.
    public func loadSessions(tenantSlug: String, limit: Int = 200) throws -> [StoredSession] {
        try queue.sync {
            let sql = """
            SELECT session_key, tenant_slug, display_title, last_message_at, created_at
            FROM sessions
            WHERE tenant_slug = ?
            ORDER BY last_message_at DESC
            LIMIT ?;
            """
            var out: [StoredSession] = []
            try withStatement(sql) { stmt in
                sqlite3_bind_text(stmt, 1, tenantSlug, -1, SQLITE_TRANSIENT_BRIDGE)
                sqlite3_bind_int(stmt, 2, Int32(limit))
                while sqlite3_step(stmt) == SQLITE_ROW {
                    let key = String(cString: sqlite3_column_text(stmt, 0))
                    let slug = String(cString: sqlite3_column_text(stmt, 1))
                    let title: String? = sqlite3_column_type(stmt, 2) == SQLITE_NULL
                        ? nil
                        : String(cString: sqlite3_column_text(stmt, 2))
                    let last = sqlite3_column_int64(stmt, 3)
                    let created = sqlite3_column_int64(stmt, 4)
                    out.append(StoredSession(
                        sessionKey: key,
                        tenantSlug: slug,
                        displayTitle: title,
                        lastMessageAtMs: last,
                        createdAtMs: created
                    ))
                }
            }
            return out
        }
    }

    /// Load all messages for one session in chronological order.
    public func loadMessages(sessionKey: String) throws -> [StoredMessage] {
        try queue.sync {
            let sql = """
            SELECT id, session_key, role, content, tool_call_id, created_at
            FROM messages
            WHERE session_key = ?
            ORDER BY created_at ASC, id ASC;
            """
            var out: [StoredMessage] = []
            try withStatement(sql) { stmt in
                sqlite3_bind_text(stmt, 1, sessionKey, -1, SQLITE_TRANSIENT_BRIDGE)
                while sqlite3_step(stmt) == SQLITE_ROW {
                    let id = sqlite3_column_int64(stmt, 0)
                    let key = String(cString: sqlite3_column_text(stmt, 1))
                    let role = String(cString: sqlite3_column_text(stmt, 2))
                    let content = String(cString: sqlite3_column_text(stmt, 3))
                    let tool: String? = sqlite3_column_type(stmt, 4) == SQLITE_NULL
                        ? nil
                        : String(cString: sqlite3_column_text(stmt, 4))
                    let created = sqlite3_column_int64(stmt, 5)
                    out.append(StoredMessage(
                        id: id,
                        sessionKey: key,
                        role: role,
                        content: content,
                        toolCallId: tool,
                        createdAtMs: created
                    ))
                }
            }
            return out
        }
    }

    /// Resync helper: given the server's view of sessions for a
    /// tenant, merge into the local cache. Server wins on
    /// `last_message_at` and `display_title`; local rows missing on
    /// the server are kept (offline drafts / pending sends).
    /// Returns the resulting unified session list, newest-first.
    public func mergeServerSessions(
        _ serverRows: [StoredSession],
        tenantSlug: String
    ) throws -> [StoredSession] {
        for row in serverRows where row.tenantSlug == tenantSlug {
            try upsertSession(row)
        }
        return try loadSessions(tenantSlug: tenantSlug)
    }

    /// Most recent `last_message_at` across all sessions for the
    /// tenant — feeds the `?since=` query when resyncing on launch.
    /// Returns `0` for an empty cache.
    public func latestSeenTimestampMs(tenantSlug: String) throws -> Int64 {
        try queue.sync {
            let sql = """
            SELECT COALESCE(MAX(last_message_at), 0) FROM sessions
            WHERE tenant_slug = ?;
            """
            var ts: Int64 = 0
            try withStatement(sql) { stmt in
                sqlite3_bind_text(stmt, 1, tenantSlug, -1, SQLITE_TRANSIENT_BRIDGE)
                if sqlite3_step(stmt) == SQLITE_ROW {
                    ts = sqlite3_column_int64(stmt, 0)
                }
            }
            return ts
        }
    }

    // MARK: - SQLite helpers

    private func exec(_ sql: String) throws {
        var err: UnsafeMutablePointer<CChar>?
        let rc = sqlite3_exec(db, sql, nil, nil, &err)
        if rc != SQLITE_OK {
            let msg = err.flatMap { String(cString: $0) } ?? "exec failed"
            sqlite3_free(err)
            throw SessionStoreError.step(sql: sql, code: rc, message: msg)
        }
    }

    /// Prepare/finalize a statement with automatic cleanup. Closure
    /// runs with the prepared statement and may bind / step.
    private func withStatement(_ sql: String, _ body: (OpaquePointer) throws -> Void) throws {
        var stmt: OpaquePointer?
        let rc = sqlite3_prepare_v2(db, sql, -1, &stmt, nil)
        guard rc == SQLITE_OK, let s = stmt else {
            let msg = lastErrMsg()
            if let s = stmt { sqlite3_finalize(s) }
            throw SessionStoreError.prepare(sql: sql, code: rc, message: msg)
        }
        defer { sqlite3_finalize(s) }
        try body(s)
    }

    private func lastErrMsg() -> String {
        guard let db = db else { return "no db" }
        return String(cString: sqlite3_errmsg(db))
    }
}
