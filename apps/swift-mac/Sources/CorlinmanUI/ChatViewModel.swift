// Phase 4 W3 C4 iter 6 — `ChatViewModel`: glue between `ChatStream`
// (network) + `SessionStore` (persistence) + `ChatView` (rendering).
//
// Iter 10 extends the model with `pendingApproval` and an
// `ApprovalClient` hook so `ApprovalSheet` can present the awaiting-
// approval prompt and POST the operator's decision back.
// `awaiting_approval` SSE frames already lift through `ChatChunk`
// (`Models.swift:54`); the view model remembers the most-recent one
// keyed by `(turnId, callId)` and clears it once the operator
// resolves it. That's the iter-10 close-out for the design test row
// `approval_sheet_snapshot` and the deferred iter-9 surface.
//
// The view model is `@MainActor` so SwiftUI mutations land on the
// main thread without ceremony. Network reads come off the
// `ChatStream` async iterator, but consumed inside a `Task { … }`
// owned by the view model — pressing the stop button cancels that
// task, which in turn cancels the URLSession data task and tears
// down the gateway-side stream (cancel flow per the design doc
// §"Cancellation"). The view model exposes that as `cancelStreaming()`
// so the ChatView's stop button doesn't have to know about Tasks.
//
// The streaming consumer is intentionally protocol-typed
// (`ChatStreamSource`) so tests can inject deterministic chunk
// sequences without spinning up URLSession. Same trick the
// `ChatStream` byte source uses — turtles all the way down.

import Foundation
#if canImport(SwiftUI)
import SwiftUI
#endif

import CorlinmanCore

/// What the view renders for one message. Distinct from
/// `StoredMessage` because the in-flight assistant message has no row
/// id yet, and we may want to surface streaming-only state (a typing
/// indicator, an awaiting-approval banner) that never makes it to
/// disk.
public struct ChatMessageVM: Identifiable, Equatable, Sendable {
    public enum Role: String, Sendable { case user, assistant, system, tool }

    public let id: UUID
    public let role: Role
    public var content: String
    public var isStreaming: Bool
    public var awaitingApprovalCallId: String?

    public init(
        id: UUID = UUID(),
        role: Role,
        content: String,
        isStreaming: Bool = false,
        awaitingApprovalCallId: String? = nil
    ) {
        self.id = id
        self.role = role
        self.content = content
        self.isStreaming = isStreaming
        self.awaitingApprovalCallId = awaitingApprovalCallId
    }
}

/// Anything that can yield a chat stream for the view model. The
/// production binding is `ChatStream` from `CorlinmanCore`; tests
/// inject a fixture that replays a hand-rolled sequence.
public protocol ChatStreamSource: Sendable {
    /// Open a stream for the given user prompt. The implementation
    /// owns request shaping (URL, auth header, body) — the view model
    /// doesn't speak HTTP.
    func openStream(for prompt: String, sessionKey: String) -> ChatStream
}

/// Awaiting-approval prompt the view model surfaces to `ApprovalSheet`.
/// One in-flight at a time — the agent serialises its tool calls so
/// queueing multiple is a future iter (post-C4) when concurrent
/// approval becomes a real shape.
public struct PendingApproval: Equatable, Sendable, Identifiable {
    public let id: String              // `callId`, unique per call
    public let turnId: String
    public let plugin: String
    public let tool: String
    public let argsPreview: String

    public init(turnId: String, callId: String, plugin: String, tool: String, argsPreview: String) {
        self.id = callId
        self.turnId = turnId
        self.plugin = plugin
        self.tool = tool
        self.argsPreview = argsPreview
    }
}

/// Drives `ChatView`. Pure logic — the SwiftUI view consumes
/// `messages` and `isStreaming` and calls `send` / `cancelStreaming`.
@MainActor
public final class ChatViewModel: ObservableObject {
    @Published public private(set) var messages: [ChatMessageVM] = []
    @Published public private(set) var isStreaming: Bool = false
    /// Surfaced to the UI so the composer's text field can show the
    /// last error inline. Cleared on the next successful send.
    @Published public private(set) var lastError: String?

    /// Iter 10 — surfaced to `ApprovalSheet`. Non-nil while the agent
    /// is blocked on operator approval; cleared when `resolveApproval`
    /// completes (or the user cancels the stream).
    @Published public private(set) var pendingApproval: PendingApproval?

    private let source: ChatStreamSource
    public let sessionKey: String
    private let store: SessionStore?
    private var streamTask: Task<Void, Never>?
    private let approvalClient: ApprovalClient?

    /// Tenant slug used when persisting new session rows. The view
    /// model itself doesn't enforce tenant scoping — the caller picks
    /// which tenant a chat belongs to and passes it in.
    public let tenantSlug: String

    public init(
        source: ChatStreamSource,
        sessionKey: String = UUID().uuidString,
        tenantSlug: String,
        store: SessionStore? = nil,
        approvalClient: ApprovalClient? = nil
    ) {
        self.source = source
        self.sessionKey = sessionKey
        self.tenantSlug = tenantSlug
        self.store = store
        self.approvalClient = approvalClient
    }

    /// Hydrate `messages` from the local cache. The view calls this
    /// in `.task` on first appear so resumed sessions render before
    /// any network roundtrip.
    public func loadFromCache() {
        guard let store = store else { return }
        do {
            let stored = try store.loadMessages(sessionKey: sessionKey)
            self.messages = stored.map { row in
                let role: ChatMessageVM.Role
                switch row.role {
                case "user": role = .user
                case "assistant": role = .assistant
                case "tool": role = .tool
                default: role = .system
                }
                return ChatMessageVM(role: role, content: row.content)
            }
        } catch {
            self.lastError = "cache load failed: \(error)"
        }
    }

    /// Send a user prompt and start streaming the assistant reply.
    /// Idempotent w.r.t. `isStreaming` — repeated taps while a
    /// stream is in flight are no-ops.
    public func send(_ prompt: String) {
        guard !isStreaming else { return }
        let trimmed = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        lastError = nil

        let now = currentTimeMs()
        // Persist + reflect the user message immediately.
        messages.append(ChatMessageVM(role: .user, content: trimmed))
        persistUserMessage(trimmed, at: now)

        // Open an empty assistant slot the streaming task fills in.
        let assistantIdx = messages.count
        messages.append(ChatMessageVM(role: .assistant, content: "", isStreaming: true))

        isStreaming = true
        let stream = source.openStream(for: trimmed, sessionKey: sessionKey)
        streamTask = Task { [weak self] in
            await self?.consume(stream: stream, assistantIndex: assistantIdx)
        }
    }

    /// Cancel an in-flight stream. Safe to call when nothing is
    /// streaming (no-op).
    public func cancelStreaming() {
        streamTask?.cancel()
        streamTask = nil
        isStreaming = false
        // A cancel mid-approval should drop the prompt so the sheet
        // dismisses (the gateway will resync the awaiting-approval
        // state on the next turn if it's still pending).
        pendingApproval = nil
        if let last = messages.last, last.role == .assistant, last.isStreaming {
            messages[messages.count - 1].isStreaming = false
        }
    }

    // MARK: - Approval

    /// Iter 10 — relay an operator decision back via
    /// `POST /v1/chat/completions/:turn_id/approve`. Returns once the
    /// gateway has acknowledged so the sheet can dismiss with a
    /// success state. The streaming task continues to run while this
    /// is in flight; the next SSE chunks drop into the open assistant
    /// slot (whether new tokens, another `awaiting_approval`, or
    /// `done`).
    ///
    /// Idempotent w.r.t. `pendingApproval`: clears the cached prompt
    /// only on success so a transient network failure leaves the
    /// sheet open for retry.
    public func resolveApproval(
        approved: Bool,
        scope: ApprovalDecision.Scope = .once,
        denyMessage: String? = nil
    ) async {
        guard let prompt = pendingApproval else { return }
        guard let client = approvalClient else {
            lastError = "approval client not configured"
            return
        }
        let decision = ApprovalDecision(
            callId: prompt.id,
            approved: approved,
            scope: scope,
            denyMessage: denyMessage
        )
        do {
            _ = try await client.submit(turnId: prompt.turnId, decision: decision)
            // Only clear on confirmed success — otherwise leave the
            // sheet up so the operator can retry.
            if pendingApproval?.id == prompt.id {
                pendingApproval = nil
            }
        } catch {
            lastError = "approve failed: \(error)"
        }
    }

    // MARK: - Stream consumption

    private func consume(stream: ChatStream, assistantIndex: Int) async {
        var assistantBuffer = ""
        var didFinish = false
        do {
            for try await chunk in stream {
                if Task.isCancelled { break }
                switch chunk {
                case .tokenDelta(_, _, let content):
                    assistantBuffer += content
                    apply(assistantBuffer: assistantBuffer, at: assistantIndex)
                case .toolCallDelta:
                    // Iter 6 doesn't surface tool-call deltas in the
                    // assistant message — they'd duplicate text and
                    // confuse the typing indicator. Iter 9's
                    // `ApprovalSheet` is the right surface for these.
                    break
                case .awaitingApproval(let turnId, let callId, let plugin, let tool, let preview):
                    let banner = "[awaiting approval] \(plugin):\(tool) — \(preview)"
                    assistantBuffer += assistantBuffer.isEmpty ? banner : "\n\(banner)"
                    apply(assistantBuffer: assistantBuffer, at: assistantIndex,
                          callId: callId)
                    // Iter 10 — surface to `ApprovalSheet`. The sheet
                    // calls `resolveApproval(...)` once the operator
                    // picks; the gateway picks up the SSE stream once
                    // the decision lands.
                    pendingApproval = PendingApproval(
                        turnId: turnId,
                        callId: callId,
                        plugin: plugin,
                        tool: tool,
                        argsPreview: preview
                    )
                case .done:
                    didFinish = true
                }
            }
        } catch is CancellationError {
            // Cancelled — assistantBuffer holds whatever streamed
            // before the cancel; persist it so the user can see what
            // they got even on stop.
        } catch {
            lastError = "\(error)"
        }
        // Finalise UI state on the main actor.
        if assistantIndex < messages.count {
            messages[assistantIndex].content = assistantBuffer
            messages[assistantIndex].isStreaming = false
        }
        if didFinish || !assistantBuffer.isEmpty {
            persistAssistantMessage(assistantBuffer, at: currentTimeMs())
        }
        isStreaming = false
        streamTask = nil
    }

    private func apply(
        assistantBuffer: String,
        at index: Int,
        callId: String? = nil
    ) {
        guard index < messages.count else { return }
        messages[index].content = assistantBuffer
        if let callId = callId {
            messages[index].awaitingApprovalCallId = callId
        }
    }

    // MARK: - Persistence

    private func persistUserMessage(_ content: String, at ts: Int64) {
        guard let store = store else { return }
        do {
            try store.upsertSession(StoredSession(
                sessionKey: sessionKey,
                tenantSlug: tenantSlug,
                displayTitle: String(content.prefix(60)),
                lastMessageAtMs: ts,
                createdAtMs: ts
            ))
            try store.appendMessage(StoredMessage(
                sessionKey: sessionKey,
                role: "user",
                content: content,
                createdAtMs: ts
            ))
        } catch {
            lastError = "persist user message: \(error)"
        }
    }

    private func persistAssistantMessage(_ content: String, at ts: Int64) {
        guard let store = store, !content.isEmpty else { return }
        do {
            try store.upsertSession(StoredSession(
                sessionKey: sessionKey,
                tenantSlug: tenantSlug,
                displayTitle: nil,    // keep existing title
                lastMessageAtMs: ts,
                createdAtMs: ts
            ))
            try store.appendMessage(StoredMessage(
                sessionKey: sessionKey,
                role: "assistant",
                content: content,
                createdAtMs: ts
            ))
        } catch {
            lastError = "persist assistant message: \(error)"
        }
    }

    private func currentTimeMs() -> Int64 {
        Int64(Date().timeIntervalSince1970 * 1000)
    }
}
