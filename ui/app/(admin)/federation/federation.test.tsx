/**
 * FederationPage tests (Phase 4 W2 B3 iter 6+).
 *
 * Covers:
 *   - Two-pane render — accepted_from + peers_of_us rows from a single fetch
 *   - Add-source form posts the typed slug through the API client
 *   - 503 tenants_disabled renders the banner instead of an error toast
 *
 * Mocks the federation API client at module scope; mirrors the discipline
 * used by the existing admin page tests under `app/(admin)/.../page.test.tsx`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nextProvider } from "react-i18next";
import * as React from "react";

import { i18next, initI18n } from "@/lib/i18n";
import type {
  AddFederationResult,
  FederationListResult,
  RecentProposalsResult,
  RemoveFederationResult,
} from "@/lib/api/federation";

// ---------------------------------------------------------------------------
// Module mock — install before importing the page.
// ---------------------------------------------------------------------------

const fetchPeersMock: ReturnType<typeof vi.fn> = vi.fn(
  async (): Promise<FederationListResult> => {
    throw new Error("fetchPeersMock not configured");
  },
);
const addPeerMock: ReturnType<typeof vi.fn> = vi.fn(
  async (_slug: string): Promise<AddFederationResult> => {
    throw new Error("addPeerMock not configured");
  },
);
const removePeerMock: ReturnType<typeof vi.fn> = vi.fn(
  async (_slug: string): Promise<RemoveFederationResult> => {
    throw new Error("removePeerMock not configured");
  },
);
const recentMock: ReturnType<typeof vi.fn> = vi.fn(
  async (_slug: string): Promise<RecentProposalsResult> => {
    throw new Error("recentMock not configured");
  },
);

vi.mock("@/lib/api/federation", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/federation")>(
    "@/lib/api/federation",
  );
  return {
    ...actual,
    fetchFederationPeers: () => fetchPeersMock(),
    addFederationPeer: (slug: string) => addPeerMock(slug),
    removeFederationPeer: (slug: string) => removePeerMock(slug),
    fetchRecentFederatedProposals: (slug: string) => recentMock(slug),
  };
});

// next/navigation — page itself doesn't read it, but defensive stub.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/federation",
  useSearchParams: () => new URLSearchParams(),
}));

import FederationPage from "./page";

// ---------------------------------------------------------------------------

beforeEach(() => {
  initI18n();
  void i18next.changeLanguage("en");
  fetchPeersMock.mockReset();
  addPeerMock.mockReset();
  removePeerMock.mockReset();
  recentMock.mockReset();
});

afterEach(() => {
  cleanup();
});

function Harness({ children }: { children: React.ReactNode }) {
  const [client] = React.useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { retry: false, refetchOnWindowFocus: false },
          mutations: { retry: false },
        },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18next}>{children}</I18nextProvider>
    </QueryClientProvider>
  );
}

describe("FederationPage", () => {
  it("federation_page_lists_accepted_from_and_peers_of_us", async () => {
    fetchPeersMock.mockResolvedValueOnce({
      kind: "ok",
      accepted_from: [
        {
          peer_tenant_id: "current",
          source_tenant_id: "bravo",
          accepted_at_ms: 1_777_000_000_000,
          accepted_by: "alice",
        },
      ],
      peers_of_us: [
        {
          peer_tenant_id: "charlie",
          source_tenant_id: "current",
          accepted_at_ms: 1_777_111_111_111,
          accepted_by: "bob",
        },
      ],
    });

    render(
      <Harness>
        <FederationPage />
      </Harness>,
    );

    await waitFor(() => {
      // Recipient pane row: keyed off the source slug.
      expect(
        screen.getByTestId("federation-accepted-from-row-bravo"),
      ).toBeInTheDocument();
      // Publishing pane row: keyed off the peer slug.
      expect(
        screen.getByTestId("federation-peers-of-us-row-charlie"),
      ).toBeInTheDocument();
    });

    // Operator names land in the rendered cells.
    const recipientRow = screen.getByTestId(
      "federation-accepted-from-row-bravo",
    );
    expect(recipientRow.textContent).toMatch(/alice/);
    const publishingRow = screen.getByTestId(
      "federation-peers-of-us-row-charlie",
    );
    expect(publishingRow.textContent).toMatch(/bob/);

    // The publishing pane is read-only — no Remove button rendered.
    expect(
      screen.queryByTestId("federation-remove-charlie"),
    ).not.toBeInTheDocument();
  });

  it("add_source_tenant_form_calls_post_with_typed_slug", async () => {
    fetchPeersMock.mockResolvedValue({
      kind: "ok",
      accepted_from: [],
      peers_of_us: [],
    });
    addPeerMock.mockResolvedValueOnce({
      kind: "ok",
      peer: {
        peer_tenant_id: "current",
        source_tenant_id: "acme",
        accepted_at_ms: 1_777_222_222_222,
        accepted_by: "admin",
      },
    });

    render(
      <Harness>
        <FederationPage />
      </Harness>,
    );

    const input = await screen.findByTestId("federation-add-input");
    fireEvent.change(input, { target: { value: "acme" } });

    const submit = screen.getByTestId("federation-add-submit");
    fireEvent.click(submit);

    await waitFor(() => {
      expect(addPeerMock).toHaveBeenCalledWith("acme");
    });
  });

  it("tenants_disabled_503_renders_banner_not_table", async () => {
    fetchPeersMock.mockResolvedValueOnce({ kind: "tenants_disabled" });

    render(
      <Harness>
        <FederationPage />
      </Harness>,
    );

    await waitFor(() => {
      expect(
        screen.getByTestId("federation-disabled-banner"),
      ).toBeInTheDocument();
    });
    // Both panes show the disabled-row hint instead of the empty/loaded state.
    expect(
      screen.getByTestId("federation-accepted-from-disabled-row"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("federation-peers-of-us-disabled-row"),
    ).toBeInTheDocument();
    // The Add form input is disabled in this state.
    const input = screen.getByTestId(
      "federation-add-input",
    ) as HTMLInputElement;
    expect(input.disabled).toBe(true);
  });
});
