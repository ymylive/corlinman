import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { UptimeStreak } from "./uptime-streak";

afterEach(() => cleanup());

describe("UptimeStreak", () => {
  const bars: Array<{ height: number; tone: "ok" | "warn" | "err" }> = Array.from(
    { length: 30 },
    (_, i) => ({
      height: 90 + ((i * 7) % 10),
      tone: i === 5 ? "warn" : i === 17 ? "err" : "ok",
    }),
  );

  it("renders the pct + label + incidents text", () => {
    render(
      <UptimeStreak pct="99.94" bars={bars} incidentsText="3 incidents total" />,
    );
    expect(screen.getByText("99.94")).toBeInTheDocument();
    expect(screen.getByText("90-day availability")).toBeInTheDocument();
    expect(screen.getByText("3 incidents total")).toBeInTheDocument();
  });

  it("renders one bar per entry in the histogram", () => {
    const { container } = render(
      <UptimeStreak pct="99.94" bars={bars} />,
    );
    const barContainer = container.querySelector('[aria-hidden="true"]:last-child');
    expect(barContainer?.children.length).toBe(bars.length);
  });

  it("marks the bar strip as aria-hidden for screen readers", () => {
    const { container } = render(<UptimeStreak pct="99.94" bars={bars} />);
    // The strip is the second aria-hidden container (first is the glow blob).
    const stripCandidates = container.querySelectorAll('[aria-hidden="true"]');
    const strip = Array.from(stripCandidates).find((el) => el.children.length === bars.length);
    expect(strip).toBeDefined();
  });
});
