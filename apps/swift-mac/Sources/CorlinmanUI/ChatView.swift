// Phase 4 W3 C4 iter 6 — `ChatView`: SwiftUI front for the streaming
// chat. Intentionally plain — the design doc explicitly defers
// styling polish to a later iter (line 500): "UI is intentionally
// plain; a future iter (or the design skill) can pretty it up."
//
// Three components compose the chat surface:
//   - `ChatView` — top-level layout: list above, composer below.
//   - `MessageList` — auto-scrolling `List` of bubbles.
//   - `Composer` — text field + send/stop button. The button toggles
//     based on `viewModel.isStreaming`; cancel calls
//     `cancelStreaming()`, send calls `send(_:)`.
//
// We use `ScrollViewReader` to scroll to the latest message every
// time `messages` mutates. Without it the user has to manually scroll
// during a long stream, which in practice means they miss the tail
// of every response — a tiny detail with outsize UX impact.

import SwiftUI

/// Top-level chat surface. Pass a configured view model from the App
/// layer so the same view recompiles for iOS without dragging
/// CorlinmanCore networking decisions into the view itself.
public struct ChatView: View {
    @ObservedObject public var viewModel: ChatViewModel
    @State private var draft: String = ""

    public init(viewModel: ChatViewModel) {
        self.viewModel = viewModel
    }

    public var body: some View {
        VStack(spacing: 0) {
            MessageList(messages: viewModel.messages)
            Divider()
            Composer(
                draft: $draft,
                isStreaming: viewModel.isStreaming,
                lastError: viewModel.lastError,
                onSend: {
                    let prompt = draft
                    draft = ""
                    viewModel.send(prompt)
                },
                onCancel: { viewModel.cancelStreaming() }
            )
        }
        .frame(minWidth: 480, minHeight: 320)
        .task { viewModel.loadFromCache() }
        // Iter 10 — pop the approval sheet whenever the view model
        // surfaces a pending awaiting-approval chunk. We `item:`-bind
        // so a fresh prompt with a different `callId` re-presents the
        // sheet without dismissing animation jank.
        .sheet(item: Binding(
            get: { viewModel.pendingApproval },
            set: { _ in /* dismiss is driven by view model */ }
        )) { prompt in
            ApprovalSheet(
                prompt: prompt,
                onResolve: { approved, scope, deny in
                    Task { await viewModel.resolveApproval(approved: approved, scope: scope, denyMessage: deny) }
                },
                onCancel: {
                    // Cancel = treat as deny with no message; gives
                    // the gateway a definitive answer instead of
                    // leaving the agent stalled.
                    Task { await viewModel.resolveApproval(approved: false, scope: .once, denyMessage: nil) }
                }
            )
        }
    }
}

/// Scrollable list of message bubbles. Auto-pins to the latest
/// message when `messages` changes.
public struct MessageList: View {
    public let messages: [ChatMessageVM]

    public init(messages: [ChatMessageVM]) {
        self.messages = messages
    }

    public var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    ForEach(messages) { message in
                        MessageBubble(message: message)
                            .id(message.id)
                            .frame(maxWidth: .infinity, alignment: bubbleAlignment(for: message.role))
                    }
                    if messages.isEmpty {
                        Text("Start the conversation below.")
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .center)
                            .padding(.top, 80)
                    }
                }
                .padding(16)
            }
            // macOS 13 compat: use the single-argument `onChange` form
            // (deprecated on 14+ but still compiles, and the new
            // `(of:initial:_:)` two-arg form is 14-only). When the
            // `Package.swift` floor moves to macOS 14, swap this back.
            .onChange(of: messages.count) { _ in
                guard let last = messages.last else { return }
                withAnimation(.easeOut(duration: 0.15)) {
                    proxy.scrollTo(last.id, anchor: .bottom)
                }
            }
        }
    }

    private func bubbleAlignment(for role: ChatMessageVM.Role) -> Alignment {
        role == .user ? .trailing : .leading
    }
}

/// A single message row. Streaming assistant messages get a trailing
/// "…" so it's obvious tokens are still flowing — cheaper than a
/// dots animation and survives snapshot tests deterministically.
public struct MessageBubble: View {
    public let message: ChatMessageVM

    public init(message: ChatMessageVM) {
        self.message = message
    }

    public var body: some View {
        VStack(alignment: alignment, spacing: 4) {
            Text(message.role.rawValue.capitalized)
                .font(.caption2)
                .foregroundStyle(.secondary)
            Text(rendered)
                .textSelection(.enabled)
                .padding(10)
                .background(background)
                .clipShape(RoundedRectangle(cornerRadius: 12))
        }
        .frame(maxWidth: 520, alignment: frameAlignment)
    }

    private var alignment: HorizontalAlignment {
        message.role == .user ? .trailing : .leading
    }

    private var frameAlignment: Alignment {
        message.role == .user ? .trailing : .leading
    }

    private var rendered: String {
        if message.isStreaming && message.content.isEmpty { return "…" }
        return message.isStreaming ? message.content + " …" : message.content
    }

    private var background: Color {
        switch message.role {
        case .user: return .blue.opacity(0.18)
        case .assistant: return .gray.opacity(0.14)
        case .tool: return .orange.opacity(0.18)
        case .system: return .secondary.opacity(0.10)
        }
    }
}

/// Composer at the bottom of the chat. The button label flips between
/// "Send" and "Stop" depending on streaming state; both states keep
/// the keyboard focus on the text field so power users can chain
/// turns without picking up the mouse.
public struct Composer: View {
    @Binding public var draft: String
    public let isStreaming: Bool
    public let lastError: String?
    public let onSend: () -> Void
    public let onCancel: () -> Void

    public init(
        draft: Binding<String>,
        isStreaming: Bool,
        lastError: String?,
        onSend: @escaping () -> Void,
        onCancel: @escaping () -> Void
    ) {
        self._draft = draft
        self.isStreaming = isStreaming
        self.lastError = lastError
        self.onSend = onSend
        self.onCancel = onCancel
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let lastError = lastError {
                Text("Error: \(lastError)")
                    .font(.footnote)
                    .foregroundStyle(.red)
            }
            HStack(spacing: 8) {
                TextField("Message", text: $draft, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...6)
                    .disabled(isStreaming)
                    .onSubmit(submit)
                Button(isStreaming ? "Stop" : "Send", action: submit)
                    .keyboardShortcut(.return, modifiers: [])
                    .buttonStyle(.borderedProminent)
                    .tint(isStreaming ? .red : .blue)
                    .disabled(!isStreaming && draft.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding(12)
        .background(.thinMaterial)
    }

    private func submit() {
        if isStreaming {
            onCancel()
        } else {
            onSend()
        }
    }
}
