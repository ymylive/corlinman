import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { ProposalCard } from "../ProposalCard";
import type { EvolutionProposal } from "../types";

const NOW = Date.UTC(2026, 3, 25, 14, 0, 0);

const PROPOSAL: EvolutionProposal = {
  id: "evo_test_01",
  kind: "memory_op",
  target: "memory/sessions/qq:test/snippets",
  diff: [
    "--- a/foo.md",
    "+++ b/foo.md",
    "@@ -1,2 +1,3 @@",
    " keep",
    "-old",
    "+new",
  ].join("\n"),
  reasoning: "Frequent user repeats — confidence 0.86.",
  risk: "low",
  status: "pending",
  signal_ids: [1, 2, 3],
  trace_ids: ["t-1", "t-2"],
  created_at: NOW - 1000 * 60 * 5,
};

describe("ProposalCard", () => {
  it("renders kind, target, age and signal count", () => {
    render(
      <ProposalCard
        proposal={PROPOSAL}
        now={NOW}
        onApprove={() => {}}
        onDeny={() => {}}
      />,
    );

    expect(screen.getByText("memory_op")).toBeInTheDocument();
    expect(
      screen.getByText("memory/sessions/qq:test/snippets"),
    ).toBeInTheDocument();
    // "5 minutes ago" under zh-CN copy.
    expect(screen.getByText(/5 分钟前提出/)).toBeInTheDocument();
    expect(screen.getByText(/信号 ×3/)).toBeInTheDocument();
  });

  it("fires onApprove when the approve button is clicked", () => {
    const onApprove = vi.fn();
    render(
      <ProposalCard
        proposal={PROPOSAL}
        now={NOW}
        onApprove={onApprove}
        onDeny={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onApprove).toHaveBeenCalledWith("evo_test_01");
  });

  it("opens the inline deny editor and forwards the reason", () => {
    const onDeny = vi.fn();
    render(
      <ProposalCard
        proposal={PROPOSAL}
        now={NOW}
        onApprove={() => {}}
        onDeny={onDeny}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Deny" }));

    // The Approve button is gone while the inline editor is open.
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();

    const input = screen.getByLabelText("拒绝理由（可选）") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "方向偏激" } });
    fireEvent.click(screen.getByRole("button", { name: "确认拒绝" }));

    expect(onDeny).toHaveBeenCalledWith("evo_test_01", "方向偏激");
  });

  it("expands to show the diff body on demand", () => {
    render(
      <ProposalCard
        proposal={PROPOSAL}
        now={NOW}
        onApprove={() => {}}
        onDeny={() => {}}
      />,
    );

    // Default collapsed — diff content not yet rendered.
    expect(screen.queryByText(/@@ -1,2 \+1,3 @@/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /展开详情/ }));
    expect(screen.getByText(/@@ -1,2 \+1,3 @@/)).toBeInTheDocument();
  });

  it("renders high-risk badge with the err tone label", () => {
    render(
      <ProposalCard
        proposal={{ ...PROPOSAL, risk: "high" }}
        now={NOW}
        onApprove={() => {}}
        onDeny={() => {}}
      />,
    );
    expect(screen.getByText("high")).toBeInTheDocument();
  });
});
