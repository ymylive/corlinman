import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, fireEvent } from "@testing-library/react";
import {
  FilterChipGroup,
  type FilterChipOption,
} from "./filter-chip-group";

afterEach(() => cleanup());

const opts: FilterChipOption[] = [
  { value: "all", label: "All", count: 12 },
  { value: "ok", label: "ok", count: 3, tone: "ok" },
  { value: "warn", label: "warn", count: 1, tone: "warn" },
  { value: "err", label: "err", count: 1, tone: "err" },
  { value: "info", label: "info", count: 7 },
];

describe("FilterChipGroup (single)", () => {
  it("marks the active chip with aria-selected and data-active", () => {
    render(
      <FilterChipGroup
        options={opts}
        value="ok"
        onChange={() => void 0}
        label="severity"
      />,
    );
    const ok = screen.getByRole("tab", { name: /ok/ });
    expect(ok).toHaveAttribute("aria-selected", "true");
    expect(ok).toHaveAttribute("data-active", "true");
  });

  it("fires onChange with the clicked value", () => {
    const onChange = vi.fn();
    render(<FilterChipGroup options={opts} value="all" onChange={onChange} />);
    fireEvent.click(screen.getByRole("tab", { name: /warn/ }));
    expect(onChange).toHaveBeenCalledWith("warn");
  });

  it("exposes role=tablist with the given label", () => {
    render(
      <FilterChipGroup
        options={opts}
        value="all"
        onChange={() => void 0}
        label="severity"
      />,
    );
    expect(screen.getByRole("tablist")).toHaveAttribute(
      "aria-label",
      "severity",
    );
  });
});

describe("FilterChipGroup (multi)", () => {
  it("toggles items in/out of the selection", () => {
    const onChange = vi.fn();
    render(
      <FilterChipGroup
        multi
        options={opts}
        value={["ok", "warn"]}
        onChange={onChange}
      />,
    );
    // Clicking "ok" should remove it
    fireEvent.click(screen.getByRole("button", { name: /ok/ }));
    expect(onChange).toHaveBeenCalledWith(["warn"]);

    // Aria-pressed reflects state
    const warn = screen.getByRole("button", { name: /warn/ });
    expect(warn).toHaveAttribute("aria-pressed", "true");
  });
});
