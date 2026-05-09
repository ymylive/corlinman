// Phase 4 W3 C4 iter 7 — `OnboardingView`: first-launch credential capture.
//
// The flow this view implements (per design doc §"Auth flow"):
//
//   1. Operator types: gateway URL, admin username, admin password.
//   2. Hit "Continue" → `POST /admin/auth/login` → session cookie lands.
//   3. `GET /admin/tenants?for_user=…` → tenant picker (auto-pick if
//      singleton).
//   4. `POST /admin/api_keys` body `{ tenant, scope: "chat" }` →
//      api_key gets stashed in Keychain.
//   5. View signals "done" via the closure the App layer passes in.
//
// The view itself is intentionally dependency-light: it takes an
// `OnboardingViewModel` (in `OnboardingViewModel.swift`) and renders
// whatever phase the model is in. Networking + persistence live in
// the view model, which in turn calls the `GatewayClient` and
// `AuthStore` from `CorlinmanCore`. Same separation `ChatView` →
// `ChatViewModel` → `ChatStream` follows.

import SwiftUI

import CorlinmanCore

/// Top-level onboarding container. Renders the right phase view
/// based on `viewModel.phase`. The App layer passes a completion
/// closure the view model calls when the operator finishes minting
/// the api_key.
public struct OnboardingView: View {
    @ObservedObject public var viewModel: OnboardingViewModel

    public init(viewModel: OnboardingViewModel) {
        self.viewModel = viewModel
    }

    public var body: some View {
        VStack(spacing: 18) {
            Text("Connect to a Corlinman gateway")
                .font(.title2).bold()
            Text("Phase 4 W3 C4 — iter 7 onboarding")
                .font(.caption)
                .foregroundStyle(.secondary)

            switch viewModel.phase {
            case .credentials:
                CredentialsForm(viewModel: viewModel)
            case .tenants(let tenants):
                TenantPickerForm(tenants: tenants, viewModel: viewModel)
            case .minting:
                ProgressView("Minting API key…")
                    .padding(.vertical, 24)
            case .done:
                VStack(spacing: 8) {
                    Image(systemName: "checkmark.seal.fill")
                        .font(.system(size: 44))
                        .foregroundStyle(.green)
                    Text("Onboarding complete.")
                        .font(.headline)
                }
                .padding(.vertical, 24)
            }

            if let err = viewModel.lastError {
                Text("Error: \(err)")
                    .font(.footnote)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 24)
            }
        }
        .padding(36)
        .frame(minWidth: 540, minHeight: 420)
    }
}

/// Phase 1: gateway URL + admin user/pass. Submit kicks the view
/// model to fetch tenants.
struct CredentialsForm: View {
    @ObservedObject var viewModel: OnboardingViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            LabelledField(
                label: "Gateway URL",
                text: $viewModel.gatewayURL,
                placeholder: "https://gateway.corlinman.local"
            )
            LabelledField(
                label: "Admin username",
                text: $viewModel.adminUsername,
                placeholder: "admin"
            )
            LabelledSecureField(
                label: "Admin password",
                text: $viewModel.adminPassword
            )
            Button {
                Task { await viewModel.submitCredentials() }
            } label: {
                HStack {
                    Spacer()
                    Text("Continue")
                    Spacer()
                }
            }
            .keyboardShortcut(.return, modifiers: [])
            .buttonStyle(.borderedProminent)
            .disabled(!viewModel.credentialsAreValid || viewModel.isWorking)
        }
    }
}

/// Phase 2: pick a tenant. Singleton list auto-confirms — the view
/// model already passed it, but we render a single-row form so the
/// operator sees what they're committing to.
struct TenantPickerForm: View {
    let tenants: [TenantSummary]
    @ObservedObject var viewModel: OnboardingViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Select a tenant")
                .font(.headline)
            Picker("Tenant", selection: $viewModel.selectedTenantSlug) {
                ForEach(tenants, id: \.slug) { t in
                    Text(t.display_name ?? t.slug).tag(t.slug as String?)
                }
            }
            .pickerStyle(.radioGroup)
            Button {
                Task { await viewModel.confirmTenant() }
            } label: {
                HStack {
                    Spacer()
                    Text("Mint API key")
                    Spacer()
                }
            }
            .keyboardShortcut(.return, modifiers: [])
            .buttonStyle(.borderedProminent)
            .disabled(viewModel.selectedTenantSlug == nil || viewModel.isWorking)
        }
    }
}

/// Reusable label + text field. The pre-built `Form` styles on macOS
/// don't lay these out the way the design intent calls for, so we
/// roll a tiny one.
struct LabelledField: View {
    let label: String
    @Binding var text: String
    let placeholder: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            TextField(placeholder, text: $text)
                .textFieldStyle(.roundedBorder)
        }
    }
}

struct LabelledSecureField: View {
    let label: String
    @Binding var text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            SecureField("", text: $text)
                .textFieldStyle(.roundedBorder)
        }
    }
}
