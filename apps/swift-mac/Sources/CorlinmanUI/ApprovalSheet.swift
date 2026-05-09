// Phase 4 W3 C4 iter 10 — `ApprovalSheet`: SwiftUI sheet for tool-
// approval prompts the agent emits mid-stream.
//
// Per the design doc §"Streaming UX" (`docs/design/phase4-w3-c4-design.md:248-256`):
//
//     when `ChatChunk.awaitingApproval` arrives, the view model
//     presents a sheet with the plugin/tool name + args preview.
//     User picks approve/deny + scope. The choice goes back as
//     `POST /v1/chat/completions/:turn_id/approve` …
//
// The sheet is intentionally minimal — the design doc explicitly
// scopes UI polish out (line 500). Three rows: prompt header, args
// preview, decision controls. The scope picker has three options;
// today the gateway treats `session` / `always` as `once` (per
// `chat_approve.rs:50-54`), but we surface the picker so the wire
// shape gets exercised. When the gateway grows scope tracking, the
// sheet doesn't change.

import SwiftUI

import CorlinmanCore

/// Sheet UI for one in-flight approval prompt. Presented modally over
/// `ChatView` whenever `ChatViewModel.pendingApproval` is non-nil. The
/// sheet calls back into the view model — no networking lives here,
/// matching the same separation `ChatView` → `ChatViewModel` →
/// `ApprovalClient` keeps elsewhere.
public struct ApprovalSheet: View {
    public let prompt: PendingApproval
    public let onResolve: (Bool, ApprovalDecision.Scope, String?) -> Void
    public let onCancel: () -> Void

    @State private var scope: ApprovalDecision.Scope = .once
    @State private var denyMessage: String = ""

    public init(
        prompt: PendingApproval,
        onResolve: @escaping (Bool, ApprovalDecision.Scope, String?) -> Void,
        onCancel: @escaping () -> Void
    ) {
        self.prompt = prompt
        self.onResolve = onResolve
        self.onCancel = onCancel
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            // Header.
            VStack(alignment: .leading, spacing: 4) {
                Text("Approve tool call?")
                    .font(.title3).bold()
                Text("\(prompt.plugin):\(prompt.tool)")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                Text("turn \(prompt.turnId) · call \(prompt.id)")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }

            Divider()

            // Args preview — the agent ships a redacted summary so we
            // can render it raw without sanitising.
            VStack(alignment: .leading, spacing: 6) {
                Text("Arguments")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                ScrollView(.vertical) {
                    Text(prompt.argsPreview.isEmpty ? "(no preview)" : prompt.argsPreview)
                        .font(.system(.footnote, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(8)
                        .background(.gray.opacity(0.1))
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                }
                .frame(maxHeight: 120)
            }

            // Scope picker. Surfaces the wire shape even though the
            // gateway treats all three the same at iter 3 stub
            // (`chat_approve.rs:50-54`).
            VStack(alignment: .leading, spacing: 4) {
                Text("Scope")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Picker("Scope", selection: $scope) {
                    Text("Once").tag(ApprovalDecision.Scope.once)
                    Text("This session").tag(ApprovalDecision.Scope.session)
                    Text("Always").tag(ApprovalDecision.Scope.always)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
            }

            // Optional deny message — required by the body schema
            // when `approved=false` (`chat_approve.rs:36`). We don't
            // enforce that client-side because the gateway can return
            // its own validation; cheaper than mirroring the rule.
            VStack(alignment: .leading, spacing: 4) {
                Text("Deny reason (optional)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                TextField("Why deny", text: $denyMessage)
                    .textFieldStyle(.roundedBorder)
            }

            Spacer(minLength: 0)

            HStack(spacing: 8) {
                Button("Cancel", role: .cancel, action: onCancel)
                Spacer()
                Button(role: .destructive) {
                    onResolve(false, scope, denyMessage.isEmpty ? nil : denyMessage)
                } label: {
                    Text("Deny")
                }
                Button {
                    onResolve(true, scope, nil)
                } label: {
                    Text("Approve")
                }
                .keyboardShortcut(.return, modifiers: [])
                .buttonStyle(.borderedProminent)
            }
        }
        .padding(20)
        .frame(minWidth: 460, minHeight: 320)
    }
}
