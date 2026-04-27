import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { MetricsDelta } from "../MetricsDelta";

describe("MetricsDelta", () => {
  it("renders one row per event_kind in the union of baseline + current", () => {
    render(
      <MetricsDelta
        baseline={{ "tool.call.failed": 4, "search.recall.dropped": 2 }}
        current={{ "tool.call.failed": 6, "prompt.eval.failed": 1 }}
      />,
    );

    // Three distinct event_kind labels.
    expect(screen.getByText("tool.call.failed")).toBeInTheDocument();
    expect(screen.getByText("search.recall.dropped")).toBeInTheDocument();
    expect(screen.getByText("prompt.eval.failed")).toBeInTheDocument();
  });

  it("annotates each row with baseline → current and the delta", () => {
    render(
      <MetricsDelta
        baseline={{ "tool.call.failed": 4 }}
        current={{ "tool.call.failed": 6 }}
      />,
    );

    // 4 -> 6, +2 (+50%).
    expect(screen.getByText(/4 → 6/)).toBeInTheDocument();
    expect(screen.getByText(/\+2 \(\+50%\)/)).toBeInTheDocument();
  });

  it("limits compact variant to the top-3 movers by abs delta", () => {
    render(
      <MetricsDelta
        baseline={{ a: 0, b: 0, c: 0, d: 0, e: 0 }}
        current={{ a: 9, b: 7, c: 5, d: 3, e: 1 }}
        variant="compact"
      />,
    );

    // Top-3 (a, b, c) survive the slice.
    expect(screen.getByText("a")).toBeInTheDocument();
    expect(screen.getByText("b")).toBeInTheDocument();
    expect(screen.getByText("c")).toBeInTheDocument();
    // d and e should be hidden in compact mode.
    expect(screen.queryByText("d")).toBeNull();
    expect(screen.queryByText("e")).toBeNull();
  });

  it("returns null when both maps are empty", () => {
    const { container } = render(
      <MetricsDelta baseline={{}} current={{}} />,
    );
    expect(container.firstChild).toBeNull();
  });
});
