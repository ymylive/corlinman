/**
 * Profile switcher tests (W3.4).
 *
 * Covers:
 *   - Renders the trigger label as the resolved profile display_name
 *   - Clicking the trigger opens the listbox + lists every profile
 *   - Picking a non-active profile flips ``localStorage`` + updates label
 *   - "Manage profiles…" routes to /profiles
 *   - Storage roundtrip survives a re-render (hydration path)
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";

const pushMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: vi.fn() }),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    listProfiles: vi.fn(),
  };
});

/**
 * jsdom ships an incomplete ``window.localStorage`` (missing ``.clear``
 * etc). Install a minimal in-memory shim — the active-profile context
 * persists the slug there.
 */
function installLocalStorageShim() {
  const store = new Map<string, string>();
  const ls: Storage = {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (k) => store.get(k) ?? null,
    key: (i) => Array.from(store.keys())[i] ?? null,
    removeItem: (k) => void store.delete(k),
    setItem: (k, v) => void store.set(k, String(v)),
  };
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    writable: true,
    value: ls,
  });
}

import { listProfiles } from "@/lib/api";
import {
  ActiveProfileProvider,
  STORAGE_KEY,
} from "@/lib/context/active-profile";
import { ProfileSwitcher } from "./profile-switcher";

const mockedList = vi.mocked(listProfiles);

const FIXTURE_PROFILES = [
  {
    slug: "default",
    display_name: "Default",
    parent_slug: null,
    description: null,
    created_at: "2026-04-01T00:00:00Z",
  },
  {
    slug: "research",
    display_name: "Research Bot",
    parent_slug: "default",
    description: null,
    created_at: "2026-05-01T00:00:00Z",
  },
];

function renderSwitcher() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ActiveProfileProvider>
        <ProfileSwitcher />
      </ActiveProfileProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  pushMock.mockReset();
  mockedList.mockReset();
  installLocalStorageShim();
});

afterEach(() => cleanup());

describe("ProfileSwitcher", () => {
  it("renders the active profile's display_name as the trigger label", async () => {
    mockedList.mockResolvedValue({ profiles: FIXTURE_PROFILES });
    renderSwitcher();
    const trigger = await screen.findByTestId("profile-switcher-trigger");
    // The seeded slug is "default" → display_name "Default".
    await waitFor(() =>
      expect(trigger.textContent ?? "").toMatch(/Default/),
    );
  });

  it("shows every profile + a Manage link on click", async () => {
    mockedList.mockResolvedValue({ profiles: FIXTURE_PROFILES });
    renderSwitcher();
    const trigger = await screen.findByTestId("profile-switcher-trigger");
    fireEvent.click(trigger);
    expect(
      await screen.findByTestId("profile-switcher-item-default"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("profile-switcher-item-research"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("profile-switcher-manage"),
    ).toBeInTheDocument();
  });

  it("writes the new slug to localStorage when picking a non-active profile", async () => {
    mockedList.mockResolvedValue({ profiles: FIXTURE_PROFILES });
    renderSwitcher();
    fireEvent.click(await screen.findByTestId("profile-switcher-trigger"));
    fireEvent.click(
      await screen.findByTestId("profile-switcher-item-research"),
    );
    await waitFor(() =>
      expect(window.localStorage.getItem(STORAGE_KEY)).toBe("research"),
    );
    // Trigger label flips to the new profile.
    const trigger = await screen.findByTestId("profile-switcher-trigger");
    await waitFor(() =>
      expect(trigger.textContent ?? "").toMatch(/Research Bot/),
    );
  });

  it("routes to /profiles when the Manage item is activated", async () => {
    mockedList.mockResolvedValue({ profiles: FIXTURE_PROFILES });
    renderSwitcher();
    fireEvent.click(await screen.findByTestId("profile-switcher-trigger"));
    fireEvent.click(
      await screen.findByTestId("profile-switcher-manage"),
    );
    expect(pushMock).toHaveBeenCalledWith("/profiles");
  });

  it("hydrates the active slug from localStorage", async () => {
    window.localStorage.setItem(STORAGE_KEY, "research");
    mockedList.mockResolvedValue({ profiles: FIXTURE_PROFILES });
    renderSwitcher();
    const trigger = await screen.findByTestId("profile-switcher-trigger");
    await waitFor(() =>
      expect(trigger.textContent ?? "").toMatch(/Research Bot/),
    );
  });

  it("snaps back to default when the stored slug points at a deleted profile", async () => {
    window.localStorage.setItem(STORAGE_KEY, "ghost");
    mockedList.mockResolvedValue({ profiles: FIXTURE_PROFILES });
    renderSwitcher();
    await waitFor(() =>
      expect(window.localStorage.getItem(STORAGE_KEY)).toBe("default"),
    );
  });
});
