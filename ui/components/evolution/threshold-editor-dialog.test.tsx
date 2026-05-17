/**
 * W4.6 — ThresholdEditorDialog
 *
 * Covers:
 *   - seeds inputs from the incoming profile
 *   - shows the inline error + disables Save when archive <= stale
 *   - shows the inline error when interval < 1
 *   - clicking Save with a valid payload calls onSave with the trio
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { ThresholdEditorDialog } from "./threshold-editor-dialog";
import type { ProfileCuratorState } from "@/lib/api";

const PROFILE: ProfileCuratorState = {
  slug: "default",
  paused: false,
  interval_hours: 168,
  stale_after_days: 30,
  archive_after_days: 90,
  last_review_at: null,
  last_review_summary: null,
  run_count: 0,
  skill_counts: { active: 0, stale: 0, archived: 0, total: 0 },
  origin_counts: {
    bundled: 0,
    "user-requested": 0,
    "agent-created": 0,
  },
};

describe("ThresholdEditorDialog", () => {
  it("seeds inputs from the profile when opened", () => {
    render(
      <ThresholdEditorDialog
        open
        onOpenChange={() => {}}
        profile={PROFILE}
        onSave={() => {}}
      />,
    );
    expect(
      (screen.getByTestId("threshold-interval") as HTMLInputElement).value,
    ).toBe("168");
    expect(
      (screen.getByTestId("threshold-stale") as HTMLInputElement).value,
    ).toBe("30");
    expect(
      (screen.getByTestId("threshold-archive") as HTMLInputElement).value,
    ).toBe("90");
  });

  it("shows an inline error when archive <= stale and disables Save", () => {
    render(
      <ThresholdEditorDialog
        open
        onOpenChange={() => {}}
        profile={PROFILE}
        onSave={() => {}}
      />,
    );

    fireEvent.change(screen.getByTestId("threshold-archive"), {
      target: { value: "30" },
    });

    expect(screen.getByTestId("threshold-error")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /保存阈值/ })).toBeDisabled();
  });

  it("shows the error when interval < 1", () => {
    render(
      <ThresholdEditorDialog
        open
        onOpenChange={() => {}}
        profile={PROFILE}
        onSave={() => {}}
      />,
    );
    fireEvent.change(screen.getByTestId("threshold-interval"), {
      target: { value: "0" },
    });
    expect(screen.getByTestId("threshold-error")).toBeInTheDocument();
  });

  it("calls onSave with the validated trio on Save click", () => {
    const onSave = vi.fn();
    render(
      <ThresholdEditorDialog
        open
        onOpenChange={() => {}}
        profile={PROFILE}
        onSave={onSave}
      />,
    );
    fireEvent.change(screen.getByTestId("threshold-stale"), {
      target: { value: "14" },
    });
    fireEvent.change(screen.getByTestId("threshold-archive"), {
      target: { value: "60" },
    });

    fireEvent.click(screen.getByRole("button", { name: /保存阈值/ }));
    expect(onSave).toHaveBeenCalledWith({
      interval_hours: 168,
      stale_after_days: 14,
      archive_after_days: 60,
    });
  });

  it("Cancel button closes the dialog", () => {
    const onOpenChange = vi.fn();
    render(
      <ThresholdEditorDialog
        open
        onOpenChange={onOpenChange}
        profile={PROFILE}
        onSave={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
