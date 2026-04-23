import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, fireEvent } from "@testing-library/react";
import {
  CommandPalette,
  type PaletteGroup,
} from "./command-palette";

// cmdk transitively reads ResizeObserver and calls scrollIntoView on the
// active item — jsdom provides neither. Polyfill both as no-ops for this
// test file.
beforeAll(() => {
  if (typeof globalThis.ResizeObserver === "undefined") {
    class MockResizeObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver =
      MockResizeObserver;
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => void 0;
  }
});

afterEach(() => cleanup());

const groups: PaletteGroup[] = [
  {
    id: "actions",
    label: "Actions",
    items: [
      {
        id: "review",
        label: "Review pending approvals",
        shortcut: "↵",
        badge: "2",
        keywords: ["approve", "queue"],
      },
      {
        id: "deny",
        label: "Deny file_write",
        shortcut: "⌘ ⌫",
        meta: "last 24h: 0",
      },
    ],
  },
  {
    id: "jump",
    label: "Jump to",
    items: [
      { id: "approvals", label: "Approvals", shortcut: "G A", meta: "2 pending" },
      { id: "logs", label: "Logs stream", shortcut: "G L" },
    ],
  },
];

describe("CommandPalette", () => {
  it("renders nothing when closed", () => {
    const { container } = render(
      <CommandPalette
        open={false}
        onOpenChange={() => void 0}
        groups={groups}
      />,
    );
    expect(container.textContent).toBe("");
  });

  it("renders all groups and items when open", () => {
    render(
      <CommandPalette
        open
        onOpenChange={() => void 0}
        groups={groups}
      />,
    );
    expect(screen.getByText("Actions")).toBeInTheDocument();
    expect(screen.getByText("Jump to")).toBeInTheDocument();
    expect(screen.getByText("Review pending approvals")).toBeInTheDocument();
    expect(screen.getByText("Approvals")).toBeInTheDocument();
    expect(screen.getByText("Logs stream")).toBeInTheDocument();
  });

  it("shows the attention badge and shortcut", () => {
    render(<CommandPalette open onOpenChange={() => void 0} groups={groups} />);
    expect(screen.getByText("2")).toBeInTheDocument();
    // shortcuts
    expect(screen.getByText("G A")).toBeInTheDocument();
    expect(screen.getByText("G L")).toBeInTheDocument();
  });

  it("closes on Esc (keyboard listener)", () => {
    const onOpenChange = vi.fn();
    render(<CommandPalette open onOpenChange={onOpenChange} groups={groups} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("closes when clicking the backdrop", () => {
    const onOpenChange = vi.fn();
    render(
      <CommandPalette open onOpenChange={onOpenChange} groups={groups} />,
    );
    const backdrop = screen.getByTestId("palette-backdrop");
    fireEvent.click(backdrop);
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("uses role=dialog + aria-modal on the card", () => {
    render(<CommandPalette open onOpenChange={() => void 0} groups={groups} />);
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAttribute("aria-label", "Command palette");
  });
});
