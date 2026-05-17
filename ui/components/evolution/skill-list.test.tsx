/**
 * W4.6 — SkillList
 *
 * Covers:
 *   - renders every skill with name + version + state/origin badges
 *   - filtering by state (dropdown) hides non-matching rows
 *   - filtering by origin (dropdown) hides non-matching rows
 *   - search box filters by case-insensitive substring
 *   - clicking the pin toggle calls onTogglePin with the inverted value
 *   - empty filtered result → empty placeholder visible
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";

import { SkillList } from "./skill-list";
import type { SkillSummary } from "@/lib/api";

const SKILLS: SkillSummary[] = [
  {
    name: "code-review",
    description: "reviews diffs",
    version: "1.0.0",
    state: "active",
    origin: "agent-created",
    pinned: false,
    use_count: 5,
    last_used_at: "2026-05-15T10:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
  },
  {
    name: "weather",
    description: "forecast helper",
    version: "0.3.0",
    state: "stale",
    origin: "user-requested",
    pinned: true,
    use_count: 0,
    last_used_at: null,
    created_at: "2026-02-01T00:00:00Z",
  },
  {
    name: "calc",
    description: "calculator",
    version: "1.0.0",
    state: "archived",
    origin: "bundled",
    pinned: false,
    use_count: 0,
    last_used_at: null,
    created_at: "2026-03-01T00:00:00Z",
  },
];

describe("SkillList", () => {
  it("renders every skill with state and origin badges", () => {
    render(<SkillList skills={SKILLS} onTogglePin={() => {}} />);
    expect(screen.getByTestId("skill-row-code-review")).toBeInTheDocument();
    expect(screen.getByTestId("skill-row-weather")).toBeInTheDocument();
    expect(screen.getByTestId("skill-row-calc")).toBeInTheDocument();
    // Badges
    expect(screen.getByTestId("skill-state-active")).toBeInTheDocument();
    expect(screen.getByTestId("skill-state-stale")).toBeInTheDocument();
    expect(screen.getByTestId("skill-state-archived")).toBeInTheDocument();
    expect(screen.getByTestId("skill-origin-agent-created")).toBeInTheDocument();
    expect(screen.getByTestId("skill-origin-user-requested")).toBeInTheDocument();
    expect(screen.getByTestId("skill-origin-bundled")).toBeInTheDocument();
  });

  it("filters by state via the state dropdown", () => {
    render(<SkillList skills={SKILLS} onTogglePin={() => {}} />);
    fireEvent.change(screen.getByTestId("skill-filter-state"), {
      target: { value: "stale" },
    });
    expect(screen.queryByTestId("skill-row-code-review")).toBeNull();
    expect(screen.getByTestId("skill-row-weather")).toBeInTheDocument();
    expect(screen.queryByTestId("skill-row-calc")).toBeNull();
  });

  it("filters by origin via the origin dropdown", () => {
    render(<SkillList skills={SKILLS} onTogglePin={() => {}} />);
    fireEvent.change(screen.getByTestId("skill-filter-origin"), {
      target: { value: "agent-created" },
    });
    expect(screen.getByTestId("skill-row-code-review")).toBeInTheDocument();
    expect(screen.queryByTestId("skill-row-weather")).toBeNull();
    expect(screen.queryByTestId("skill-row-calc")).toBeNull();
  });

  it("filters by case-insensitive substring search", () => {
    render(<SkillList skills={SKILLS} onTogglePin={() => {}} />);
    fireEvent.change(screen.getByTestId("skill-search"), {
      target: { value: "FORECAST" },
    });
    expect(screen.queryByTestId("skill-row-code-review")).toBeNull();
    expect(screen.getByTestId("skill-row-weather")).toBeInTheDocument();
  });

  it("shows the empty placeholder when no rows match", () => {
    render(<SkillList skills={SKILLS} onTogglePin={() => {}} />);
    fireEvent.change(screen.getByTestId("skill-search"), {
      target: { value: "nothing-matches-this" },
    });
    expect(screen.getByTestId("skill-list-empty")).toBeInTheDocument();
  });

  it("calls onTogglePin with the inverted pin value", () => {
    const onTogglePin = vi.fn();
    render(<SkillList skills={SKILLS} onTogglePin={onTogglePin} />);

    // code-review starts unpinned → click should flip to true
    fireEvent.click(screen.getByTestId("pin-toggle-code-review"));
    expect(onTogglePin).toHaveBeenCalledWith("code-review", true);

    // weather starts pinned → click should flip to false
    fireEvent.click(screen.getByTestId("pin-toggle-weather"));
    expect(onTogglePin).toHaveBeenCalledWith("weather", false);
  });

  it("expands the row to show the description", () => {
    render(<SkillList skills={SKILLS} onTogglePin={() => {}} />);
    const row = screen.getByTestId("skill-row-code-review");
    // Click the row toggle (the inner button)
    fireEvent.click(within(row).getByRole("button", { expanded: false }));
    expect(within(row).getByText("reviews diffs")).toBeInTheDocument();
  });
});
