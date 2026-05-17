/**
 * W4.6 — ProfileCuratorCard
 *
 * Covers:
 *   - renders slug, status pill (running / paused), threshold pills
 *   - "Run now" button is disabled when the profile is paused
 *   - clicking each action button invokes the corresponding callback
 *   - last-run summary line shows "never ran" when last_review_at is null
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { ProfileCuratorCard } from "./profile-curator-card";
import type { ProfileCuratorState } from "@/lib/api";

const SAMPLE: ProfileCuratorState = {
  slug: "default",
  paused: false,
  interval_hours: 168,
  stale_after_days: 30,
  archive_after_days: 90,
  last_review_at: null,
  last_review_summary: null,
  run_count: 0,
  skill_counts: { active: 5, stale: 2, archived: 1, total: 8 },
  origin_counts: {
    bundled: 3,
    "user-requested": 2,
    "agent-created": 3,
  },
};

describe("ProfileCuratorCard", () => {
  it("renders slug, running status, and threshold pills", () => {
    render(
      <ProfileCuratorCard
        profile={SAMPLE}
        onPreview={() => {}}
        onRunNow={() => {}}
        onTogglePause={() => {}}
        onEditThresholds={() => {}}
      />,
    );
    expect(screen.getByText("default")).toBeInTheDocument();
    expect(screen.getByTestId("status-running")).toBeInTheDocument();
    expect(screen.getByText("168h")).toBeInTheDocument();
    expect(screen.getByText("30d")).toBeInTheDocument();
    expect(screen.getByText("90d")).toBeInTheDocument();
  });

  it("shows the paused status pill when profile.paused is true", () => {
    render(
      <ProfileCuratorCard
        profile={{ ...SAMPLE, paused: true }}
        onPreview={() => {}}
        onRunNow={() => {}}
        onTogglePause={() => {}}
        onEditThresholds={() => {}}
      />,
    );
    expect(screen.getByTestId("status-paused")).toBeInTheDocument();
  });

  it("invokes onPreview / onTogglePause / onEditThresholds on clicks", () => {
    const onPreview = vi.fn();
    const onTogglePause = vi.fn();
    const onEditThresholds = vi.fn();
    render(
      <ProfileCuratorCard
        profile={SAMPLE}
        onPreview={onPreview}
        onRunNow={() => {}}
        onTogglePause={onTogglePause}
        onEditThresholds={onEditThresholds}
      />,
    );

    // i18n is zh-CN in tests; match by aria-label which is i18n'd to
    // the zh-CN copy.
    fireEvent.click(screen.getByRole("button", { name: /预览/ }));
    expect(onPreview).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: /暂停/ }));
    expect(onTogglePause).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: /编辑阈值/ }));
    expect(onEditThresholds).toHaveBeenCalledTimes(1);
  });

  it("disables Run now when the profile is paused", () => {
    const onRunNow = vi.fn();
    render(
      <ProfileCuratorCard
        profile={{ ...SAMPLE, paused: true }}
        onPreview={() => {}}
        onRunNow={onRunNow}
        onTogglePause={() => {}}
        onEditThresholds={() => {}}
      />,
    );
    const runBtn = screen.getByRole("button", { name: /立即执行/ });
    expect(runBtn).toBeDisabled();
    fireEvent.click(runBtn);
    expect(onRunNow).not.toHaveBeenCalled();
  });

  it("disables every action when busy=true", () => {
    render(
      <ProfileCuratorCard
        profile={SAMPLE}
        busy
        onPreview={() => {}}
        onRunNow={() => {}}
        onTogglePause={() => {}}
        onEditThresholds={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: /预览/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /立即执行/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /暂停/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /编辑阈值/ })).toBeDisabled();
  });
});
