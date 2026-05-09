// Phase 4 W3 C4 iter 8 — `PushReceiver`: APNs delegate + dev-socket
// fallback wrapped in one `AsyncSequence<PushNotification>` surface.
//
// Two transports, one consumer. The view layer subscribes to a single
// async sequence and doesn't care whether the payload originated as
// an APNs push (production) or a JSON line on a Unix socket (dev).
// This matches the design doc §"Push surface":
//
//     `PushReceiver` exposes `AsyncSequence<PushNotification>`, and
//     `OnboardingView` / `AppDelegate` consume it the same way
//     regardless of source.
//
// ### Why we ship the dev variant first
//
// Real APNs needs an Apple Developer Program membership, a signed +
// notarised binary, a P8 auth key on the gateway side, and at minimum
// a sandbox device token. None of that is available on a fresh CI
// runner or a contributor's clone. The dev socket fallback keeps the
// iteration loop unblocked. Iter 10's smoke test uses the dev socket
// path; APNs proper is exercised only when an operator opts in.
//
// ### Wire shape
//
// Both transports decode into `PushNotification`. The fields mirror
// the protobuf message proposed in the design doc §"Push surface":
//   id, tenant_id, user_id, kind, title, body, deep_link, created_at_ms.
// We use Codable JSON because (a) APNs payloads are JSON anyway, and
// (b) shipping a fresh `.proto` for a four-field shape is busy-work.
// If the field set grows we'll codegen — but not before.

import Foundation

/// One push notification, transport-agnostic.
public struct PushNotification: Codable, Equatable, Sendable {
    public enum Kind: String, Codable, Sendable {
        case approvalRequired = "PUSH_KIND_APPROVAL_REQUIRED"
        case taskCompleted = "PUSH_KIND_TASK_COMPLETED"
        case evolutionApplied = "PUSH_KIND_EVOLUTION_APPLIED"
        case unspecified = "PUSH_KIND_UNSPECIFIED"
    }

    public let id: String
    public let tenant_id: String
    public let user_id: String
    public let kind: Kind
    public let title: String
    public let body: String
    public let deep_link: String?
    public let created_at_ms: Int64

    public init(
        id: String,
        tenant_id: String,
        user_id: String,
        kind: Kind,
        title: String,
        body: String,
        deep_link: String? = nil,
        created_at_ms: Int64
    ) {
        self.id = id
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.kind = kind
        self.title = title
        self.body = body
        self.deep_link = deep_link
        self.created_at_ms = created_at_ms
    }
}

/// Errors surfaced by `PushReceiver`. Distinct from the per-transport
/// errors so the consumer can rely on one match table.
public enum PushReceiverError: Error, Sendable {
    /// Could not open the dev socket — typically because the path
    /// doesn't exist or isn't a Unix-domain socket.
    case socketOpen(path: String, errno: Int32)
    /// Socket reads / writes hit an OS-level failure.
    case socketIO(errno: Int32)
    /// JSON decoding failed for an inbound line. The line is
    /// surfaced raw for log diagnostics.
    case decodeFailed(line: String, underlying: Error)
}

/// Common surface every push transport implements. The view layer
/// only ever sees this protocol — APNs vs. dev-socket pickling
/// happens in the constructor.
public protocol PushSource: Sendable {
    /// Yields one `PushNotification` per inbound payload. Cancelling
    /// the consumer's `Task` tears down the underlying transport.
    func notifications() -> AsyncStream<PushNotification>
}

/// Top-level façade. Pick the variant via the `transport` initialiser
/// argument — production uses `.apns`, dev/CI uses `.devSocket(path:)`,
/// tests use `.fixture(_:)`.
public final class PushReceiver: PushSource, @unchecked Sendable {
    public enum Transport {
        case devSocket(path: String)
        case apns(adapter: APNsTokenAdapter)
        case fixture([PushNotification])
    }

    private let transport: Transport

    public init(transport: Transport) {
        self.transport = transport
    }

    public func notifications() -> AsyncStream<PushNotification> {
        switch transport {
        case .devSocket(let path):
            return DevSocketPushSource(path: path).notifications()
        case .apns(let adapter):
            return adapter.incoming
        case .fixture(let items):
            return AsyncStream { continuation in
                Task {
                    for item in items {
                        continuation.yield(item)
                    }
                    continuation.finish()
                }
            }
        }
    }
}

/// APNs-side token + payload bridge. The Apple lifecycle delivers
/// device tokens via `application(_:didRegisterForRemoteNotifications…)`
/// and pushes via the user-notifications delegate. The adapter wraps
/// both behind two streams the App layer feeds and `PushReceiver`
/// drains.
///
/// We keep this in `CorlinmanCore` (instead of `CorlinmanApp`) so the
/// view-layer tests can construct one with synthetic events without
/// importing AppKit.
public final class APNsTokenAdapter: @unchecked Sendable {
    private let lock = NSLock()
    private var token: Data?
    private let pushContinuation: AsyncStream<PushNotification>.Continuation
    private let tokenContinuation: AsyncStream<Data>.Continuation
    public let incoming: AsyncStream<PushNotification>
    public let tokens: AsyncStream<Data>

    public init() {
        var pushCont: AsyncStream<PushNotification>.Continuation!
        var tokCont: AsyncStream<Data>.Continuation!
        self.incoming = AsyncStream { c in pushCont = c }
        self.tokens = AsyncStream { c in tokCont = c }
        self.pushContinuation = pushCont
        self.tokenContinuation = tokCont
    }

    /// Called by the AppDelegate adaptor when APNs hands back a
    /// device token.
    public func didRegisterDeviceToken(_ token: Data) {
        lock.lock()
        self.token = token
        lock.unlock()
        tokenContinuation.yield(token)
    }

    /// Most-recently-seen device token, hex-encoded for sending to
    /// the gateway via `POST /v1/devices`. `nil` until APNs first
    /// hands one over.
    public var hexDeviceToken: String? {
        lock.lock(); defer { lock.unlock() }
        return token?.map { String(format: "%02x", $0) }.joined()
    }

    /// Decode an APNs push payload (the userInfo dict the system
    /// delivers) and yield it on `incoming`. Two payload shapes are
    /// supported:
    ///
    ///   1. Top-level JSON matching `PushNotification` (gateway sends
    ///      our schema directly inside the `aps`-extension namespace).
    ///   2. APNs canonical form — `aps.alert.title` + `aps.alert.body`
    ///      with our extension fields at the top level. Used when an
    ///      operator wires the gateway through a third-party APNs
    ///      provider that flattens custom keys.
    public func deliverApnsUserInfo(_ userInfo: [AnyHashable: Any]) {
        if let payload = decodeNative(userInfo) {
            pushContinuation.yield(payload)
            return
        }
        if let payload = decodeApnsCanonical(userInfo) {
            pushContinuation.yield(payload)
        }
    }

    private func decodeNative(_ userInfo: [AnyHashable: Any]) -> PushNotification? {
        guard let data = try? JSONSerialization.data(withJSONObject: userInfo) else {
            return nil
        }
        return try? JSONDecoder().decode(PushNotification.self, from: data)
    }

    private func decodeApnsCanonical(_ userInfo: [AnyHashable: Any]) -> PushNotification? {
        guard let aps = userInfo["aps"] as? [String: Any] else { return nil }
        let alert = aps["alert"] as? [String: Any]
        let title = (alert?["title"] as? String) ?? ""
        let body = (alert?["body"] as? String) ?? ""
        guard let id = userInfo["id"] as? String,
              let tenant = userInfo["tenant_id"] as? String,
              let user = userInfo["user_id"] as? String,
              let kindRaw = userInfo["kind"] as? String,
              let kind = PushNotification.Kind(rawValue: kindRaw),
              let createdMs = userInfo["created_at_ms"] as? Int64
        else { return nil }
        return PushNotification(
            id: id,
            tenant_id: tenant,
            user_id: user,
            kind: kind,
            title: title,
            body: body,
            deep_link: userInfo["deep_link"] as? String,
            created_at_ms: createdMs
        )
    }
}

/// Dev-socket transport. Reads JSON-lines off a Unix-domain socket
/// the gateway writes to (`<data_dir>/dev_push.sock`). Iter 8 ships
/// only the *reader* — the gateway's writer side stubs out at
/// `[channels.dev_push] enabled = false` until the operator opts in.
///
/// ### Why DispatchSourceRead instead of `URLSession`
///
/// URLSession can't speak `AF_UNIX`. We could lean on `Network.framework`
/// (`NWConnection` with a `path:` endpoint) but its async API stalls
/// when the writer takes more than a few seconds between lines —
/// which the gateway's sparse push cadence guarantees. A vanilla
/// POSIX socket + `DispatchSource` is ~80 lines and does not stall.
final class DevSocketPushSource: PushSource, @unchecked Sendable {
    private let path: String

    init(path: String) { self.path = path }

    func notifications() -> AsyncStream<PushNotification> {
        AsyncStream { continuation in
            // All socket work runs in a detached `Task` so the caller
            // can cancel it cleanly. We don't pin the runloop or
            // touch DispatchIO — the read loop polls with a 100ms
            // budget so cancellation latency stays bounded.
            let task = Task.detached(priority: .utility) { [path] in
                await DevSocketPushSource.runLoop(path: path, continuation: continuation)
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    private static func runLoop(
        path: String,
        continuation: AsyncStream<PushNotification>.Continuation
    ) async {
        // Open and connect.
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        if fd < 0 {
            continuation.finish()
            return
        }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(path.utf8)
        // sockaddr_un.sun_path is 104 bytes on macOS; bail if path
        // would overflow rather than truncate silently.
        guard pathBytes.count < MemoryLayout.size(ofValue: addr.sun_path) else {
            close(fd)
            continuation.finish()
            return
        }
        withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
            ptr.withMemoryRebound(to: CChar.self, capacity: pathBytes.count + 1) { cptr in
                for (i, b) in pathBytes.enumerated() { cptr[i] = CChar(b) }
                cptr[pathBytes.count] = 0
            }
        }
        let connectStatus = withUnsafePointer(to: &addr) { ptr -> Int32 in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sptr in
                connect(fd, sptr, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        if connectStatus != 0 {
            close(fd)
            continuation.finish()
            return
        }

        var buffer = [UInt8](repeating: 0, count: 4096)
        var carry: [UInt8] = []
        let decoder = JSONDecoder()
        while !Task.isCancelled {
            let n = buffer.withUnsafeMutableBufferPointer { ptr -> Int in
                read(fd, ptr.baseAddress, ptr.count)
            }
            if n <= 0 { break }
            for i in 0..<n {
                let byte = buffer[i]
                if byte == 0x0A {
                    if let line = String(bytes: carry, encoding: .utf8),
                       !line.isEmpty,
                       let payload = try? decoder.decode(PushNotification.self, from: Data(line.utf8)) {
                        continuation.yield(payload)
                    }
                    carry.removeAll(keepingCapacity: true)
                } else {
                    carry.append(byte)
                }
            }
        }
        close(fd)
        continuation.finish()
    }
}
