/**
 * ReplayDialog tests (Phase 4 W2 4-2D).
 *
 * Covers:
 *   - Dialog opens for a session and renders the breadcrumb chain
 *   - Transcript renders with role-based styling (user / assistant rows)
 *   - 404 from the API surfaces an inline "session not found" block
 *   - 503 from the API surfaces the disabled banner inside the dialog
 *   - Rerun mode can be selected and renders generated assistant output
 *   - Close button fires onClose
 *
 * The Sessions API client (`@/lib/api/sessions`) is mocked at the module
 * level so we can assert behavior without standing up a fetch stub —
 * mirrors the discipline of `tests/a11y-audit.test.tsx`.
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

import {
  RERUN_NOT_IMPLEMENTED,
  type ReplayResult,
  type SessionSummary,
} from "@/lib/api/sessions";

// ---------------------------------------------------------------------------
// Module mock — install before importing the SUT.
// ---------------------------------------------------------------------------

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
    replaySession: (key: string, opts?: { mode?: "transcript" | "rerun" }) =>
      replayMock(key, opts),
  };
});

import { ReplayDialog } from "./replay-dialog";

// ---------------------------------------------------------------------------

beforeEach(() => {
  initI18n();
  i18next.changeLanguage("en");
  replayMock.mockReset();
});

afterEach(() => {
  cleanup();
});

const SAMPLE_SESSION: SessionSummary = {
  session_key: "qq:1234",
  last_message_at: 1_777_593_600_000,
  message_count: 12,
};

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

describe("ReplayDialog", () => {
  it("opens for a session and renders the Admin / Sessions / <key> crumb", async () => {
    replayMock.mockResolvedValueOnce({
      kind: "ok",
      replay: {
        session_key: "qq:1234",
        mode: "transcript",
        transcript: [],
        summary: { message_count: 0, tenant_id: "default" },
      },
    });

    render(
      <Harness>
        <ReplayDialog session={SAMPLE_SESSION} onClose={vi.fn()} />
      </Harness>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("replay-breadcrumbs")).toBeInTheDocument();
    });
    expect(screen.getByTestId("replay-breadcrumb-key")).toHaveTextContent(
      "qq:1234",
    );
  });

  it("renders alternating role-based rows for transcript messages", async () => {
    replayMock.mockResolvedValueOnce({
      kind: "ok",
      replay: {
        session_key: "qq:1234",
        mode: "transcript",
        transcript: [
          { role: "user", content: "hi", ts: "2026-04-30T01:02:03Z" },
          {
            role: "assistant",
            content: "hello",
            ts: "2026-04-30T01:02:04Z",
          },
          {
            role: "user",
            content: "thanks",
            ts: "2026-04-30T01:02:05Z",
          },
        ],
        summary: { message_count: 3, tenant_id: "default" },
      },
    });

    render(
      <Harness>
        <ReplayDialog session={SAMPLE_SESSION} onClose={vi.fn()} />
      </Harness>,
    );

    const list = await screen.findByTestId("transcript-list");
    expect(list).toBeInTheDocument();
    const rows = screen.getAllByTestId(/^transcript-row-/);
    expect(rows).toHaveLength(3);
    expect(rows[0]?.getAttribute("data-role")).toBe("user");
    expect(rows[1]?.getAttribute("data-role")).toBe("assistant");
    expect(rows[2]?.getAttribute("data-role")).toBe("user");
  });

  it("renders an inline 'session not found' block on 404", async () => {
    replayMock.mockResolvedValueOnce({
      kind: "not_found",
      session_key: "qq:1234",
    });

    render(
      <Harness>
        <ReplayDialog session={SAMPLE_SESSION} onClose={vi.fn()} />
      </Harness>,
    );

    const block = await screen.findByTestId("replay-not-found");
    expect(block).toBeInTheDocument();
    expect(block.textContent).toMatch(/qq:1234/);
  });

  it("renders the disabled block on 503 sessions_disabled", async () => {
    replayMock.mockResolvedValueOnce({ kind: "disabled" });

    render(
      <Harness>
        <ReplayDialog session={SAMPLE_SESSION} onClose={vi.fn()} />
      </Harness>,
    );

    const block = await screen.findByTestId("replay-disabled");
    expect(block).toBeInTheDocument();
  });

  it("renders the rerun stub explainer when rerun_diff is the sentinel", async () => {
    // Even though the dialog only requests transcript mode in v1, Agent A's
    // route may still return a rerun-stub if the operator forces the mode
    // server-side. The component keys off the sentinel, not the request.
    replayMock.mockResolvedValueOnce({
      kind: "ok",
      replay: {
        session_key: "qq:1234",
        mode: "rerun",
        transcript: [],
        summary: {
          message_count: 0,
          tenant_id: "default",
          rerun_diff: RERUN_NOT_IMPLEMENTED,
        },
      },
    });

    render(
      <Harness>
        <ReplayDialog session={SAMPLE_SESSION} onClose={vi.fn()} />
      </Harness>,
    );

    expect(await screen.findByTestId("replay-rerun-stub")).toBeInTheDocument();
  });

  it("runs rerun mode and renders generated assistant output", async () => {
    replayMock.mockResolvedValueOnce({
      kind: "ok",
      replay: {
        session_key: "qq:1234",
        mode: "transcript",
        transcript: [],
        summary: { message_count: 0, tenant_id: "default" },
      },
    });
    replayMock.mockResolvedValueOnce({
      kind: "ok",
      replay: {
        session_key: "qq:1234",
        mode: "rerun",
        transcript: [],
        summary: {
          message_count: 0,
          tenant_id: "default",
          rerun_diff: "changed",
        },
        rerun: {
          finish_reason: "stop",
          generated: [
            {
              role: "assistant",
              content: "fresh answer",
            },
          ],
        },
      },
    });

    render(
      <Harness>
        <ReplayDialog session={SAMPLE_SESSION} onClose={vi.fn()} />
      </Harness>,
    );

    const rerun = await screen.findByTestId("replay-mode-rerun");
    expect(rerun).not.toBeDisabled();
    fireEvent.click(rerun);

    await waitFor(() => {
      expect(replayMock).toHaveBeenLastCalledWith("qq:1234", { mode: "rerun" });
    });
    expect(await screen.findByTestId("replay-rerun-generated")).toHaveTextContent(
      "fresh answer",
    );
  });

  it("fires onClose when the close button is pressed", async () => {
    replayMock.mockResolvedValueOnce({
      kind: "ok",
      replay: {
        session_key: "qq:1234",
        mode: "transcript",
        transcript: [],
        summary: { message_count: 0, tenant_id: "default" },
      },
    });
    const onClose = vi.fn();
    render(
      <Harness>
        <ReplayDialog session={SAMPLE_SESSION} onClose={onClose} />
      </Harness>,
    );

    const close = await screen.findByTestId("replay-dialog-close");
    fireEvent.click(close);
    expect(onClose).toHaveBeenCalled();
  });
});
