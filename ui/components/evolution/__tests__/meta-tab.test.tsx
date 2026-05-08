/**
 * Phase 4 W2 B1 iter 6+7 — Meta proposal review surface.
 *
 * Three behavioural assertions:
 *   1. The Meta tab filters the same `pending` query down to rows whose
 *      `kind` is one of the four meta kinds defined by
 *      `EvolutionKind::is_meta()` (engine_config, engine_prompt,
 *      observer_filter, cluster_threshold).
 *   2. Apply on `engine_prompt` is gated behind a 2-step confirm — step 1
 *      requires the operator to type the full proposal id; the Apply
 *      button stays disabled until `typedId === proposal.id`.
 *   3. When `applyEvolutionProposal` rejects with a 403 carrying the
 *      gateway's `meta_approver_required` envelope, the dialog renders
 *      the actionable inline-help block (config.toml hint), NOT a
 *      generic toast.
 */

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";

import {
  CorlinmanApiError,
  type BudgetSnapshot,
  type EvolutionProposal,
} from "@/lib/api";

const fetchEvolutionPendingMock = vi.fn<() => Promise<EvolutionProposal[]>>();
const fetchEvolutionApprovedMock = vi.fn<() => Promise<EvolutionProposal[]>>();
const fetchEvolutionHistoryMock = vi.fn<() => Promise<unknown[]>>();
const fetchBudgetMock = vi.fn<() => Promise<BudgetSnapshot>>();
const applyEvolutionProposalMock = vi.fn<(id: string) => Promise<unknown>>();

vi.mock("@/lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchEvolutionPending: () => fetchEvolutionPendingMock(),
    fetchEvolutionApproved: () => fetchEvolutionApprovedMock(),
    fetchEvolutionHistory: () => fetchEvolutionHistoryMock(),
    fetchBudget: () => fetchBudgetMock(),
    applyEvolutionProposal: (id: string) => applyEvolutionProposalMock(id),
    approveEvolutionProposal: vi.fn(),
    denyEvolutionProposal: vi.fn(),
  };
});

import EvolutionPage from "@/app/(admin)/evolution/page";

const NOW = Date.UTC(2026, 4, 8, 12, 0, 0);

function makeProposal(over: Partial<EvolutionProposal>): EvolutionProposal {
  return {
    id: "evo_default",
    kind: "memory_op",
    target: "memory/sessions/default/snippets",
    diff: "",
    reasoning: "Default reasoning.",
    risk: "low",
    status: "pending",
    signal_ids: [],
    trace_ids: [],
    created_at: NOW - 1_000 * 60,
    ...over,
  };
}

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  fetchEvolutionPendingMock.mockReset();
  fetchEvolutionApprovedMock.mockReset();
  fetchEvolutionHistoryMock.mockReset();
  fetchBudgetMock.mockReset();
  applyEvolutionProposalMock.mockReset();
  fetchEvolutionApprovedMock.mockResolvedValue([]);
  fetchEvolutionHistoryMock.mockResolvedValue([]);
  fetchBudgetMock.mockResolvedValue({
    enabled: false,
    window_start_ms: 0,
    window_end_ms: 0,
    weekly_total: { limit: 0, used: 0, remaining: 0 },
    per_kind: [],
  });
});

afterEach(() => {
  cleanup();
});

describe("Phase 4 W2 B1 iter 6+7 — meta tab", () => {
  it("meta_tab_filters_to_meta_kinds_only — given mixed pending list, only meta rows render", async () => {
    const memoryOpRow = makeProposal({
      id: "evo_memory_aaa",
      kind: "memory_op",
      target: "memory/sessions/qq:test/snippets",
    });
    const enginePromptRow = makeProposal({
      id: "evo_prompt_bbb",
      kind: "engine_prompt",
      target: "engine.prompts.clustering",
      diff: JSON.stringify({
        prompt_id: "clustering",
        previous_text: "old",
        proposed_text: "new",
        reason: "tighter",
      }),
    });
    const observerFilterRow = makeProposal({
      id: "evo_obs_ccc",
      kind: "observer_filter",
      target: "tool.call.failed",
      diff: JSON.stringify({
        event_kind_pattern: "tool.call.failed",
        previous_filter: { keep: true },
        proposed_filter: { keep: false },
        reason: "noisy",
      }),
    });

    fetchEvolutionPendingMock.mockResolvedValue([
      memoryOpRow,
      enginePromptRow,
      observerFilterRow,
    ]);

    renderWithClient(<EvolutionPage />);

    // Wait for the page to settle into the post-fetch state.
    await waitFor(() => {
      expect(
        screen.getByRole("tab", { name: /Meta/ }),
      ).toBeInTheDocument();
    });

    // Click into the Meta tab.
    fireEvent.click(screen.getByRole("tab", { name: /Meta/ }));

    // Meta-rows region should appear once the tab swaps.
    const rowsRegion = await screen.findByTestId("meta-rows");
    const rowsScope = within(rowsRegion);

    // Both meta rows render.
    expect(rowsScope.getByText("#evo_prompt_bbb")).toBeInTheDocument();
    expect(rowsScope.getByText("#evo_obs_ccc")).toBeInTheDocument();

    // The non-meta row stays hidden in the meta tab.
    expect(rowsScope.queryByText("#evo_memory_aaa")).toBeNull();

    // Two Review buttons — one per meta row.
    expect(rowsScope.getAllByRole("button", { name: "Review" })).toHaveLength(
      2,
    );
  });

  it("engine_prompt_apply_requires_id_typing — Apply stays disabled until id matches", async () => {
    const enginePromptRow = makeProposal({
      id: "evo_prompt_typed_test",
      kind: "engine_prompt",
      target: "engine.prompts.clustering",
      diff: JSON.stringify({
        prompt_id: "clustering",
        previous_text: "old prompt body",
        proposed_text: "new prompt body",
        reason: "tighter",
      }),
    });

    fetchEvolutionPendingMock.mockResolvedValue([enginePromptRow]);
    applyEvolutionProposalMock.mockResolvedValue({
      id: enginePromptRow.id,
      status: "applied",
    });

    renderWithClient(<EvolutionPage />);

    await waitFor(() => {
      expect(
        screen.getByRole("tab", { name: /Meta/ }),
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("tab", { name: /Meta/ }));

    // Open the dialog via the Review button.
    const review = await screen.findByRole("button", { name: "Review" });
    fireEvent.click(review);

    // The dialog opens with the review surface — Apply (review step) is enabled.
    const reviewApply = await screen.findByRole("button", {
      name: /Apply/,
    });
    fireEvent.click(reviewApply);

    // Step 1: id-typing surface. Apply must be present but disabled.
    const idInput = await screen.findByLabelText(/提案 ID/);
    const step1Apply = screen.getByRole("button", { name: /Apply/ });
    expect(step1Apply).toBeDisabled();

    // Partial / wrong typing leaves Apply disabled.
    fireEvent.change(idInput, { target: { value: "evo_prompt_typed_te" } });
    expect(step1Apply).toBeDisabled();

    fireEvent.change(idInput, { target: { value: "wrong" } });
    expect(step1Apply).toBeDisabled();

    // Exact match enables Apply.
    fireEvent.change(idInput, { target: { value: "evo_prompt_typed_test" } });
    expect(step1Apply).not.toBeDisabled();

    // Click Apply → step 2 ("irreversible — apply?"). The applier hasn't
    // fired yet because step 2 is the second confirm.
    fireEvent.click(step1Apply);
    expect(applyEvolutionProposalMock).not.toHaveBeenCalled();

    // The step-2 surface mentions irreversibility (zh-CN copy).
    expect(
      await screen.findByText(/不可逆，需要手动回滚才能撤销/),
    ).toBeInTheDocument();
  });

  it("meta_approver_required_403_renders_inline_help — 403 envelope renders inline, not toasted", async () => {
    const enginePromptRow = makeProposal({
      id: "evo_prompt_403",
      kind: "engine_prompt",
      target: "engine.prompts.clustering",
      diff: JSON.stringify({
        prompt_id: "clustering",
        previous_text: "old",
        proposed_text: "new",
        reason: "tighter",
      }),
    });

    fetchEvolutionPendingMock.mockResolvedValue([enginePromptRow]);

    // The gateway returns 403 with the JSON envelope as the body.
    const gatewayBody = JSON.stringify({
      error: "meta_approver_required",
      user: "alice",
      kind: "engine_prompt",
    });
    applyEvolutionProposalMock.mockRejectedValue(
      new CorlinmanApiError(gatewayBody, 403, "trace-1"),
    );

    renderWithClient(<EvolutionPage />);

    await waitFor(() => {
      expect(
        screen.getByRole("tab", { name: /Meta/ }),
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("tab", { name: /Meta/ }));

    // Drive the dialog through the engine_prompt double-confirm flow.
    fireEvent.click(await screen.findByRole("button", { name: "Review" }));
    fireEvent.click(await screen.findByRole("button", { name: /Apply/ }));

    // Step 1 — type the id and click Apply to advance.
    const idInput = await screen.findByLabelText(/提案 ID/);
    fireEvent.change(idInput, { target: { value: "evo_prompt_403" } });
    fireEvent.click(screen.getByRole("button", { name: /Apply/ }));

    // Step 2 — click the final confirm button (zh-CN: "应用"), which
    // fires the rejecting mutation.
    const finalApply = await screen.findByRole("button", { name: "应用" });
    fireEvent.click(finalApply);

    await waitFor(() => {
      expect(applyEvolutionProposalMock).toHaveBeenCalledWith(
        "evo_prompt_403",
      );
    });

    // Inline-help: the dialog body contains the actionable
    // `meta_approver_users` instruction with the current user echoed back.
    // zh-CN copy: "你没有 meta 提案的审批权限。请在 config.toml 的
    // `[admin].meta_approver_users` 中加入你的用户名。（当前用户：alice）"
    const help = await screen.findByTestId("meta-approver-required");
    expect(help).toHaveTextContent(/没有 meta 提案的审批权限/);
    expect(help).toHaveTextContent(/meta_approver_users/);
    expect(help).toHaveTextContent(/config\.toml/);
    expect(help).toHaveTextContent(/alice/);
  });
});
