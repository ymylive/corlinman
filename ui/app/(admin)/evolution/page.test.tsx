/**
 * W4.6 — Evolution page (curator section)
 *
 * Smoke-tests the curator section of the rewritten /evolution page:
 *
 *   - renders the summary count from the curator query
 *   - lists one ProfileCuratorCard per profile
 *   - clicking Preview triggers a preview fetch then renders the dialog
 *
 * We mock the entire api module so the page never hits a real gateway.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import {
  cleanup,
  render,
  screen,
  waitFor,
  fireEvent,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";

import type {
  BudgetSnapshot,
  CuratorProfilesResponse,
  CuratorReport,
  EvolutionProposal,
  HistoryEntry,
  SkillsListResponse,
} from "@/lib/api";

const listCuratorProfilesMock = vi.fn<
  () => Promise<CuratorProfilesResponse>
>();
const previewCuratorRunMock = vi.fn<(slug: string) => Promise<CuratorReport>>();
const runCuratorNowMock = vi.fn<(slug: string) => Promise<CuratorReport>>();
const listProfileSkillsMock = vi.fn<
  (slug: string) => Promise<SkillsListResponse>
>();
const fetchEvolutionPendingMock = vi.fn<() => Promise<EvolutionProposal[]>>();
const fetchBudgetMock = vi.fn<() => Promise<BudgetSnapshot>>();
const fetchEvolutionHistoryMock = vi.fn<() => Promise<HistoryEntry[]>>();

vi.mock("@/lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    listCuratorProfiles: () => listCuratorProfilesMock(),
    previewCuratorRun: (s: string) => previewCuratorRunMock(s),
    runCuratorNow: (s: string) => runCuratorNowMock(s),
    listProfileSkills: (s: string) => listProfileSkillsMock(s),
    pauseCurator: vi.fn(),
    pinSkill: vi.fn(),
    updateCuratorThresholds: vi.fn(),
    fetchEvolutionPending: () => fetchEvolutionPendingMock(),
    fetchBudget: () => fetchBudgetMock(),
    fetchEvolutionHistory: () => fetchEvolutionHistoryMock(),
    approveEvolutionProposal: vi.fn(),
    denyEvolutionProposal: vi.fn(),
  };
});

import EvolutionPage from "@/app/(admin)/evolution/page";

const PROFILES: CuratorProfilesResponse = {
  profiles: [
    {
      slug: "default",
      paused: false,
      interval_hours: 168,
      stale_after_days: 30,
      archive_after_days: 90,
      last_review_at: null,
      last_review_summary: null,
      run_count: 0,
      skill_counts: { active: 5, stale: 1, archived: 1, total: 7 },
      origin_counts: {
        bundled: 3,
        "user-requested": 1,
        "agent-created": 3,
      },
    },
  ],
};

const PREVIEW_REPORT: CuratorReport = {
  profile_slug: "default",
  started_at: "2026-05-17T00:00:00Z",
  finished_at: "2026-05-17T00:00:00Z",
  duration_ms: 5,
  transitions: [
    {
      skill_name: "code-review",
      from_state: "active",
      to_state: "stale",
      reason: "stale_threshold",
      days_idle: 35,
    },
  ],
  marked_stale: 1,
  archived: 0,
  reactivated: 0,
  checked: 7,
  skipped: 3,
};

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("EvolutionPage curator section", () => {
  beforeEach(() => {
    listCuratorProfilesMock.mockResolvedValue(PROFILES);
    previewCuratorRunMock.mockResolvedValue(PREVIEW_REPORT);
    runCuratorNowMock.mockResolvedValue(PREVIEW_REPORT);
    listProfileSkillsMock.mockResolvedValue({ skills: [] });
    fetchEvolutionPendingMock.mockResolvedValue([]);
    fetchBudgetMock.mockResolvedValue({
      enabled: false,
      window_start_ms: 0,
      window_end_ms: 0,
      weekly_total: { limit: 0, used: 0, remaining: 0 },
      per_kind: [],
    });
    fetchEvolutionHistoryMock.mockResolvedValue([]);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("renders the curator section with one profile card", async () => {
    renderWithClient(<EvolutionPage />);
    await waitFor(() => {
      expect(screen.getByTestId("curator-section")).toBeInTheDocument();
      expect(screen.getByTestId("profile-card-default")).toBeInTheDocument();
    });
    // Summary line should mention the profile + counts.
    expect(screen.getByTestId("curator-summary").textContent).toMatch(
      /1 个 profile/,
    );
  });

  it("preview button fetches a dry-run report and opens the dialog", async () => {
    renderWithClient(<EvolutionPage />);
    await waitFor(() =>
      expect(screen.getByTestId("profile-card-default")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: /预览/ }));

    await waitFor(() => {
      expect(previewCuratorRunMock).toHaveBeenCalledWith("default");
      expect(screen.getByTestId("transition-code-review")).toBeInTheDocument();
    });
  });
});
