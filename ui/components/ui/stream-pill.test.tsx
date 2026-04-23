import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, fireEvent } from "@testing-library/react";
import { StreamPill } from "./stream-pill";

afterEach(() => cleanup());

describe("StreamPill", () => {
  it("renders live with breathing dot", () => {
    const { container } = render(<StreamPill state="live" rate="41.2/s" />);
    expect(screen.getByText("Live")).toBeInTheDocument();
    expect(screen.getByText("· 41.2/s")).toBeInTheDocument();
    expect(container.querySelector(".tp-breathe")).not.toBeNull();
  });

  it("renders throttled with amber breathing", () => {
    const { container } = render(<StreamPill state="throttled" />);
    expect(screen.getByText("Throttled")).toBeInTheDocument();
    expect(container.querySelector(".tp-breathe-amber")).not.toBeNull();
  });

  it("renders paused without breathing", () => {
    const { container } = render(<StreamPill state="paused" />);
    expect(screen.getByText("Paused")).toBeInTheDocument();
    expect(container.querySelector(".tp-breathe")).toBeNull();
    expect(container.querySelector(".tp-breathe-amber")).toBeNull();
  });

  it("shows pause button when live + onToggle is given", () => {
    const onToggle = vi.fn();
    render(<StreamPill state="live" onToggle={onToggle} />);
    const btn = screen.getByRole("button", { name: /Pause stream/i });
    fireEvent.click(btn);
    expect(onToggle).toHaveBeenCalledWith("live");
  });

  it("uses role=status + aria-live=polite", () => {
    render(<StreamPill state="live" />);
    const el = screen.getByRole("status");
    expect(el).toHaveAttribute("aria-live", "polite");
  });
});
