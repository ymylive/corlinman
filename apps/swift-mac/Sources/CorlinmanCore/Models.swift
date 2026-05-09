// Phase 4 W3 C4 iter 4 — wire models for the chat-stream parser.
//
// These mirror the gateway's SSE chunk shapes
// (`rust/crates/corlinman-gateway/src/routes/chat.rs:1495-1543`) — the
// gateway emits OpenAI-style `chat.completion.chunk` JSON inside `data:`
// frames, terminated by `data: [DONE]`. Decoding lives here (not in
// `ChatStream.swift`) so other surfaces — replay, tests, and a future
// non-streaming inspector — share the same Codable types instead of
// re-deriving them.
//
// Design choice: `ChatChunk` is a Swift enum with associated values
// (one case per `ServerFrame.kind` variant in `proto/corlinman/v1/agent.proto`),
// not a struct-with-optional-fields. The enum forces every consumer
// (`ChatViewModel.apply(_:)`, `ApprovalSheet`, the snapshot tests) to
// `switch` exhaustively — adding a `usage` chunk later is a compile-
// time fan-out, not a silent missed branch. Trade-off: decoding from
// JSON needs a custom `init(from:)` because the discriminator lives in
// `choices[0].delta` shape, not a tagged-union `kind` field. That extra
// 30 lines pays for itself the first time someone forgets to handle a
// new variant.
//
// The `awaitingApproval` chunk is **not** an OpenAI shape — the
// gateway extends the SSE wire with a custom `event: awaiting_approval`
// frame in iter 5+. Until that's wired we tolerate its absence; the
// tests assert today's three flavours (token, tool_call, done) plus the
// not-yet-shipped approval frame so when the gateway side lands, the
// Swift client decodes it without changes.

import Foundation

/// A single decoded chunk pulled off the gateway's SSE stream.
///
/// Mirrors `proto/corlinman/v1/agent.proto:110-119` (`ServerFrame.kind`)
/// and the OpenAI streaming envelope the gateway wraps it in. One Swift
/// case per logical event so consumers can `switch` exhaustively.
public enum ChatChunk: Equatable, Sendable {
    /// Streaming token delta — appended to the assistant's message buffer.
    /// `id` and `model` echo the values from the chunk envelope; today
    /// they're stable across a single turn but we surface them so the
    /// view model can correlate against `turn_id`.
    case tokenDelta(id: String, model: String, content: String)

    /// One slot of the assistant's `tool_calls[index]` array. Multiple
    /// chunks with the same `index` accumulate (`name` then `arguments`,
    /// or `arguments` split across multiple frames) — the consumer is
    /// responsible for merging by index per the OpenAI streaming spec.
    case toolCallDelta(id: String, model: String, index: Int, callId: String, name: String, argumentsFragment: String)

    /// Custom frame — the gateway pushes `event: awaiting_approval`
    /// alongside `data: {…}` when the agent stalls on
    /// `AwaitingApproval` (`agent.proto:137-143`). Iter 9 wires it to
    /// `ApprovalSheet`. Carries enough context to render the prompt
    /// without an extra round-trip to the gateway.
    case awaitingApproval(turnId: String, callId: String, plugin: String, tool: String, argsPreview: String)

    /// Terminal sentinel. `finish_reason` is whatever the upstream
    /// model returned, normalised to OpenAI's set
    /// (`stop` | `length` | `tool_calls` | `error`) in `chat.rs:1549`.
    case done(finishReason: String?)
}

// MARK: - Codable

/// Top-level decode entry point used by `ChatStream`. Given the JSON
/// payload of a single SSE `data:` line (without the `data: ` prefix),
/// returns the matching `ChatChunk` — or `nil` for the literal `[DONE]`
/// sentinel which the caller handles separately so the AsyncSequence
/// can terminate cleanly.
///
/// The decoder is intentionally tolerant of unknown fields: the
/// gateway may grow `usage` / `system_fingerprint` chunks before the
/// Swift client knows about them, and dropping them silently is safer
/// than tearing down a live chat over a forward-compat addition.
public struct ChatChunkDecoder: Sendable {
    public init() {}

    /// `data:` line payload (without the prefix). Returns `nil` for
    /// `[DONE]`; throws `DecodingError` for malformed JSON; returns a
    /// `.tokenDelta` with empty content for shapes we don't recognise
    /// (so the stream survives forward-compat surprises).
    public func decode(dataLine: String, eventName: String? = nil) throws -> ChatChunk? {
        let trimmed = dataLine.trimmingCharacters(in: .whitespaces)
        if trimmed == "[DONE]" {
            return .done(finishReason: nil)
        }
        guard let bytes = trimmed.data(using: .utf8) else {
            return nil
        }

        // Custom event types take precedence — the gateway's
        // `awaiting_approval` frame doesn't match the OpenAI envelope.
        if eventName == "awaiting_approval" {
            return try decodeApproval(bytes)
        }

        let envelope = try JSONDecoder().decode(ChunkEnvelope.self, from: bytes)
        guard let choice = envelope.choices.first else {
            // No choices array → treat as forward-compat unknown chunk.
            return .tokenDelta(id: envelope.id, model: envelope.model, content: "")
        }

        // tool_calls take priority over content — a single delta can in
        // principle carry both, but the gateway emits them as separate
        // chunks (see `chat.rs:1294-1330`). If both arrive in one
        // envelope we surface the tool_call (rarer, more consequential).
        if let toolCalls = choice.delta.tool_calls, let first = toolCalls.first {
            let argsFragment = first.function?.arguments ?? ""
            return .toolCallDelta(
                id: envelope.id,
                model: envelope.model,
                index: first.index,
                callId: first.id ?? "",
                name: first.function?.name ?? "",
                argumentsFragment: argsFragment
            )
        }

        if let finish = choice.finish_reason {
            return .done(finishReason: finish)
        }

        let content = choice.delta.content ?? ""
        return .tokenDelta(id: envelope.id, model: envelope.model, content: content)
    }

    private func decodeApproval(_ bytes: Data) throws -> ChatChunk {
        let payload = try JSONDecoder().decode(ApprovalEnvelope.self, from: bytes)
        return .awaitingApproval(
            turnId: payload.turn_id,
            callId: payload.call_id,
            plugin: payload.plugin,
            tool: payload.tool,
            argsPreview: payload.args_preview ?? ""
        )
    }
}

// MARK: - Wire shapes (private)

/// OpenAI `chat.completion.chunk` envelope — the gateway emits this for
/// every token delta and tool-call delta (`chat.rs:1495-1543`).
private struct ChunkEnvelope: Decodable {
    let id: String
    let model: String
    let choices: [Choice]

    struct Choice: Decodable {
        let index: Int
        let delta: Delta
        let finish_reason: String?
    }

    struct Delta: Decodable {
        let role: String?
        let content: String?
        let tool_calls: [ToolCallSlot]?
    }

    struct ToolCallSlot: Decodable {
        let index: Int
        let id: String?
        let type: String?
        let function: ToolCallFunction?
    }

    struct ToolCallFunction: Decodable {
        let name: String?
        let arguments: String?
    }
}

/// Custom approval-frame envelope. Schema is provisional; lands as
/// soon as the gateway adds `event: awaiting_approval` SSE emission
/// (tracked separately from C4 iter 4-6).
private struct ApprovalEnvelope: Decodable {
    let turn_id: String
    let call_id: String
    let plugin: String
    let tool: String
    let args_preview: String?
}
