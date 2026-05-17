/**
 * W4.6 — PreviewDialog
 *
 * Covers:
 *   - renders summary line with transition counts
 *   - lists each transition as "name: from → to (reason)"
 *   - "Apply now" CTA invokes onApply
 *   - empty transitions list → empty message + Apply disabled
 *   - loading state shows the loading placeholder, no transitions
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { PreviewDialog } from "./preview-dialog";
import type { CuratorReport } from "@/lib/api";

const NONEMPTY_REPORT: CuratorReport = {
  profile_slug: "default",
  started_at: "2026-05-17T00:00:00Z",
  finished_at: "2026-05-17T00:00:00Z",
  duration_ms: 12,
  transitions: [
    {
      skill_name: "code-review",
      from_state: "active",
      to_state: "stale",
      reason: "stale_threshold",
      days_idle: 35.2,
    },
    {
      skill_name: "weather",
      from_state: "stale",
      to_state: "archived",
      reason: "archive_threshold",
      days_idle: 100,
    },
  ],
  marked_stale: 1,
  archived: 1,
  reactivated: 0,
  checked: 8,
  skipped: 4,
};

const EMPTY_REPORT: CuratorReport = {
  profile_slug: "default",
  started_at: "2026-05-17T00:00:00Z",
  finished_at: "2026-05-17T00:00:00Z",
  duration_ms: 1,
  transitions: [],
  marked_stale: 0,
  archived: 0,
  reactivated: 0,
  checked: 8,
  skipped: 8,
};

describe("PreviewDialog", () => {
  it("lists each transition with from → to + reason", () => {
    render(
      <PreviewDialog
        open
        onOpenChange={() => {}}
        report={NONEMPTY_REPORT}
        onApply={() => {}}
      />,
    );
    expect(screen.getByTestId("transition-code-review")).toBeInTheDocument();
    expect(screen.getByTestId("transition-weather")).toBeInTheDocument();
    // Summary line counts
    expect(screen.getByText(/检查 8 项/)).toBeInTheDocument();
  });

  it("fires onApply when Apply now is clicked", () => {
    const onApply = vi.fn();
    render(
      <PreviewDialog
        open
        onOpenChange={() => {}}
        report={NONEMPTY_REPORT}
        onApply={onApply}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /立即应用/ }));
    expect(onApply).toHaveBeenCalledTimes(1);
  });

  it("disables Apply now when transitions are empty", () => {
    render(
      <PreviewDialog
        open
        onOpenChange={() => {}}
        report={EMPTY_REPORT}
        onApply={() => {}}
      />,
    );
    expect(screen.getByText(/无需变更/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /立即应用/ })).toBeDisabled();
  });

  it("shows the loading placeholder while loading=true", () => {
    render(
      <PreviewDialog
        open
        onOpenChange={() => {}}
        report={null}
        loading
        onApply={() => {}}
      />,
    );
    expect(screen.getByText(/正在生成预览/)).toBeInTheDocument();
    expect(screen.queryByTestId("transition-code-review")).toBeNull();
  });

  it("Cancel calls onOpenChange(false)", () => {
    const onOpenChange = vi.fn();
    render(
      <PreviewDialog
        open
        onOpenChange={onOpenChange}
        report={NONEMPTY_REPORT}
        onApply={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
