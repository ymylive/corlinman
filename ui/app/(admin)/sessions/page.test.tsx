/**
 * SessionsPage tests (Phase 4 W2 4-2D).
 *
 * Covers:
 *   - List rendering — rows, message count, formatted timestamp
 *   - Empty state when the API returns zero sessions
 *   - 503 sessions_disabled banner mirrors the W1 4-1B `tenants_disabled` shape
 *   - Replay button opens the dialog (we mock `replaySession` so the dialog
 *     paints synchronously)
 *
 * Mocks the Sessions API client at module scope; mirrors the discipline used
 * by the existing admin page tests under `app/(admin)/.../page.test.tsx`.
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
  ReplayResult,
  SessionsListResult,
} from "@/lib/api/sessions";

// ---------------------------------------------------------------------------
// Module mock — install before importing the page.
// ---------------------------------------------------------------------------

const fetchMock: ReturnType<typeof vi.fn> = vi.fn(
  async (): Promise<SessionsListResult> => {
    throw new Error("fetchMock not configured");
  },
);
const replayMock: ReturnType<typeof vi.fn> = vi.fn(
  async (): Promise<ReplayResult> => {
    throw new Error("replayMock not configured");
  },
);

vi.mock("@/lib/api/sessions", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/sessions")>(
    "@/lib/api/sessions",
  );
  return {
    ...actual,
    fetchSessions: () => fetchMock(),
    replaySession: (key: string, opts?: { mode?: "transcript" | "rerun" }) =>
      replayMock(key, opts),
  };
});

// next/navigation — page itself doesn't use it directly but the dialog +
// breadcrumbs layer might. Stub for safety.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/sessions",
  useSearchParams: () => new URLSearchParams(),
}));

import SessionsPage from "./page";

// ---------------------------------------------------------------------------

beforeEach(() => {
  initI18n();
  i18next.changeLanguage("en");
  fetchMock.mockReset();
  replayMock.mockReset();
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

describe("SessionsPage", () => {
  it("renders rows for each session returned by the API", async () => {
    fetchMock.mockResolvedValueOnce({
      kind: "ok",
      sessions: [
        {
          session_key: "qq:1234",
          last_message_at: 1_777_593_600_000,
          message_count: 12,
        },
        {
          session_key: "telegram:9001",
          last_message_at: 1_777_500_000_000,
          message_count: 6,
        },
      ],
    });

    render(
      <Harness>
        <SessionsPage />
      </Harness>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("session-row-qq:1234")).toBeInTheDocument();
      expect(
        screen.getByTestId("session-row-telegram:9001"),
      ).toBeInTheDocument();
    });
    // Message-count cell renders as a plain number.
    const row = screen.getByTestId("session-row-qq:1234");
    expect(row.textContent).toMatch(/12/);
  });

  it("renders the empty state when the API returns no sessions", async () => {
    fetchMock.mockResolvedValueOnce({ kind: "ok", sessions: [] });

    render(
      <Harness>
        <SessionsPage />
      </Harness>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("sessions-empty")).toBeInTheDocument();
    });
  });

  it("renders the 'session storage is off' banner on 503 sessions_disabled", async () => {
    fetchMock.mockResolvedValueOnce({ kind: "disabled" });

    render(
      <Harness>
        <SessionsPage />
      </Harness>,
    );

    await waitFor(() => {
      expect(
        screen.getByTestId("sessions-disabled-banner"),
      ).toBeInTheDocument();
    });
    expect(screen.getByTestId("sessions-disabled-row")).toBeInTheDocument();
  });

  it("opens the replay dialog when the Replay button is clicked", async () => {
    fetchMock.mockResolvedValueOnce({
      kind: "ok",
      sessions: [
        {
          session_key: "qq:1234",
          last_message_at: 1_777_593_600_000,
          message_count: 1,
        },
      ],
    });
    replayMock.mockResolvedValueOnce({
      kind: "ok",
      replay: {
        session_key: "qq:1234",
        mode: "transcript",
        transcript: [
          { role: "user", content: "hello", ts: "2026-04-30T01:02:03Z" },
        ],
        summary: { message_count: 1, tenant_id: "default" },
      },
    });

    render(
      <Harness>
        <SessionsPage />
      </Harness>,
    );

    const button = await screen.findByTestId("session-replay-qq:1234");
    fireEvent.click(button);
    // Dialog body is rendered into a portal; the breadcrumb is the cheapest
    // marker that the dialog opened.
    await waitFor(() => {
      expect(screen.getByTestId("replay-dialog")).toBeInTheDocument();
    });
    expect(replayMock).toHaveBeenCalledWith("qq:1234", { mode: "transcript" });
  });

  it("renders the load-failed cell when the query rejects", async () => {
    fetchMock.mockRejectedValueOnce(new Error("network down"));

    render(
      <Harness>
        <SessionsPage />
      </Harness>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("sessions-load-failed")).toBeInTheDocument();
    });
  });
});
