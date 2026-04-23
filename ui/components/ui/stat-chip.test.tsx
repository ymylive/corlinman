import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { StatChip } from "./stat-chip";

afterEach(() => cleanup());

describe("StatChip", () => {
  it("renders label, value, delta, and foot", () => {
    render(
      <StatChip
        label="Requests · 24h"
        value="38,214"
        delta={{ label: "↑ 12.4%", tone: "up" }}
        foot="p50 124ms"
      />,
    );
    expect(screen.getByText("Requests · 24h")).toBeInTheDocument();
    expect(screen.getByText("38,214")).toBeInTheDocument();
    expect(screen.getByText("↑ 12.4%")).toBeInTheDocument();
    expect(screen.getByText("p50 124ms")).toBeInTheDocument();
  });

  it("shows live badge on primary variant", () => {
    render(
      <StatChip label="Requests · 24h" value="38,214" variant="primary" />,
    );
    expect(screen.getByText("live")).toBeInTheDocument();
  });

  it("renders sparkline path when sparkPath is provided", () => {
    const { container } = render(
      <StatChip
        label="Requests"
        value="38,214"
        sparkPath="M0 28 L300 4"
        sparkTone="amber"
      />,
    );
    const path = container.querySelector("path");
    expect(path).not.toBeNull();
    expect(path?.getAttribute("d")).toContain("M0 28");
  });

  it("wraps in a GlassPanel with the subtle variant by default", () => {
    // Phase 6 perf: non-primary stat chips use the `subtle` GlassPanel
    // variant (no backdrop-filter) so a Dashboard row stays under the
    // ≤5 blur-layer budget. Primary chips still earn the blur (below).
    const { container } = render(
      <StatChip label="foo" value="1" data-testid="chip" />,
    );
    const panel = container.querySelector("[data-glass-variant]");
    expect(panel).toHaveAttribute("data-glass-variant", "subtle");
  });

  it("wraps in a GlassPanel with the primary variant when variant=primary", () => {
    const { container } = render(
      <StatChip label="foo" value="1" variant="primary" />,
    );
    const panel = container.querySelector("[data-glass-variant]");
    expect(panel).toHaveAttribute("data-glass-variant", "primary");
  });
});
