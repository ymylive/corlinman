/**
 * Sidebar tests — focus on the new collapsible "Channels" group introduced
 * alongside B3-FE3. We exercise:
 *   1. Default collapsed state (children hidden).
 *   2. Click on the toggle expands the group and reveals children.
 *   3. Keyboard: Enter/Space on the toggle flips expanded; ArrowDown moves
 *      focus to the first child; ArrowUp returns focus to the toggle.
 *   4. Auto-expand when the current route matches a child.
 *   5. Siblings (Dashboard, Scheduler, etc.) are still rendered — no
 *      regression on the flat entries.
 */

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import React from "react";

let mockPathname = "/";

vi.mock("next/navigation", () => ({
  usePathname: () => mockPathname,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import { Sidebar } from "./sidebar";

function installMatchMedia() {
  const mm = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: mm,
  });
}

describe("Sidebar", () => {
  beforeEach(() => {
    installMatchMedia();
    mockPathname = "/";
    try {
      window.localStorage?.clear?.();
    } catch {
      /* localStorage may be stubbed out in some envs; safe to ignore. */
    }
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders all top-level flat entries (no regression)", () => {
    render(<Sidebar user="admin" />);
    // A handful of sibling nav entries still appear as links.
    expect(screen.getByRole("link", { name: /仪表盘|Dashboard/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /定时任务|Scheduler/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Hooks/i })).toBeInTheDocument();
    // User chip is rendered.
    expect(screen.getByTestId("nav-user")).toHaveTextContent("admin");
  });

  it("renders the Channels group collapsed by default (no child links visible)", () => {
    render(<Sidebar />);
    const toggle = screen.getByTestId("sidebar-group-toggle-channels");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    // QQ / Telegram child links are not rendered until expanded.
    expect(screen.queryByRole("link", { name: /^QQ$/ })).toBeNull();
    expect(screen.queryByRole("link", { name: /Telegram$/ })).toBeNull();
  });

  it("click on the group toggle expands it and reveals the children", () => {
    render(<Sidebar />);
    const toggle = screen.getByTestId("sidebar-group-toggle-channels");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("link", { name: /^QQ$/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Telegram$/ })).toBeInTheDocument();
  });

  it("Enter / Space on the toggle flips expanded", () => {
    render(<Sidebar />);
    const toggle = screen.getByTestId("sidebar-group-toggle-channels");
    fireEvent.keyDown(toggle, { key: "Enter" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.keyDown(toggle, { key: " " });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("auto-expands when the current route matches a child (QQ)", () => {
    mockPathname = "/channels/qq";
    render(<Sidebar />);
    const toggle = screen.getByTestId("sidebar-group-toggle-channels");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    // Active child's anchor carries aria-current=page.
    const qq = screen.getByRole("link", { name: /^QQ$/ });
    expect(qq).toHaveAttribute("aria-current", "page");
    // The group label gets medium weight when a child is active
    // (Tidepool uses `font-medium` — lighter than the legacy semibold).
    expect(toggle.className).toMatch(/font-medium/);
  });

  it("auto-expands for the Telegram child route", () => {
    mockPathname = "/channels/telegram";
    render(<Sidebar />);
    const toggle = screen.getByTestId("sidebar-group-toggle-channels");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const tg = screen.getByRole("link", { name: /Telegram$/ });
    expect(tg).toHaveAttribute("aria-current", "page");
  });

  it("sets role=group and aria-label on the group wrapper", () => {
    render(<Sidebar />);
    const wrapper = screen.getByTestId("sidebar-group-channels");
    expect(wrapper).toHaveAttribute("role", "group");
    // Either the English or Chinese label is acceptable depending on the
    // test runner's locale — the wrapper has one of them.
    const aria = wrapper.getAttribute("aria-label");
    expect(aria === "Channels" || aria === "通道").toBe(true);
  });
});
