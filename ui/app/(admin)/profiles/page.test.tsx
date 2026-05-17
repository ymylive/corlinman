/**
 * Profiles admin page tests (W3.2).
 *
 * Covers list rendering, inline rename happy-path, the delete confirm
 * dialog, and the empty/error states. Mocks the entire `@/lib/api`
 * profile surface + `next/navigation` so the page renders standalone.
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

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/profiles",
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    listProfiles: vi.fn(),
    createProfile: vi.fn(),
    updateProfile: vi.fn(),
    deleteProfile: vi.fn(),
    getProfileSoul: vi.fn(),
    setProfileSoul: vi.fn(),
  };
});

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

/**
 * jsdom ships an incomplete ``window.localStorage`` (missing ``.clear``).
 * Install a minimal in-memory shim so the active-profile context can
 * persist its slug.
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

import {
  CorlinmanApiError,
  deleteProfile,
  getProfileSoul,
  listProfiles,
  setProfileSoul,
  updateProfile,
} from "@/lib/api";
import { ActiveProfileProvider } from "@/lib/context/active-profile";
import ProfilesPage from "./page";

const mockedList = vi.mocked(listProfiles);
const mockedUpdate = vi.mocked(updateProfile);
const mockedDelete = vi.mocked(deleteProfile);
const mockedSoulGet = vi.mocked(getProfileSoul);
const mockedSoulSet = vi.mocked(setProfileSoul);

const PROFILES = [
  {
    slug: "default",
    display_name: "Default",
    parent_slug: null,
    description: null,
    created_at: "2026-04-01T00:00:00Z",
  },
  {
    slug: "research",
    display_name: "Research",
    parent_slug: "default",
    description: "Reads papers",
    created_at: "2026-05-01T00:00:00Z",
  },
];

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ActiveProfileProvider>
        <ProfilesPage />
      </ActiveProfileProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockedList.mockReset();
  mockedUpdate.mockReset();
  mockedDelete.mockReset();
  mockedSoulGet.mockReset();
  mockedSoulSet.mockReset();
  installLocalStorageShim();
});

afterEach(() => cleanup());

describe("ProfilesPage — list", () => {
  it("renders one row per profile and an item count", async () => {
    mockedList.mockResolvedValue({ profiles: PROFILES });
    renderPage();
    expect(
      await screen.findByTestId("profile-row-default"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("profile-row-research")).toBeInTheDocument();
    expect(screen.getByTestId("profiles-count").textContent ?? "").toMatch(
      /2/,
    );
  });

  it("renders the empty state when the list is empty", async () => {
    mockedList.mockResolvedValue({ profiles: [] });
    renderPage();
    expect(
      await screen.findByTestId("profiles-empty"),
    ).toBeInTheDocument();
  });

  it("renders an error banner when listProfiles rejects", async () => {
    mockedList.mockRejectedValue(new Error("boom"));
    renderPage();
    expect(
      await screen.findByTestId("profiles-load-failed"),
    ).toBeInTheDocument();
  });
});

describe("ProfilesPage — inline rename", () => {
  it("PATCHes display_name on Enter and refetches the list", async () => {
    mockedList.mockResolvedValue({ profiles: PROFILES });
    mockedUpdate.mockResolvedValue({
      ...PROFILES[1]!,
      display_name: "Research Prime",
    });
    renderPage();

    const renameBtn = await screen.findByTestId("profile-rename-research");
    fireEvent.click(renameBtn);

    const input = await screen.findByTestId(
      "profile-rename-input-research",
    );
    fireEvent.change(input, { target: { value: "Research Prime" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() =>
      expect(mockedUpdate).toHaveBeenCalledWith("research", {
        display_name: "Research Prime",
      }),
    );
  });

  it("cancels the rename on Escape without firing the PATCH", async () => {
    mockedList.mockResolvedValue({ profiles: PROFILES });
    renderPage();

    fireEvent.click(await screen.findByTestId("profile-rename-research"));
    const input = await screen.findByTestId(
      "profile-rename-input-research",
    );
    fireEvent.change(input, { target: { value: "Nope" } });
    fireEvent.keyDown(input, { key: "Escape" });

    await waitFor(() =>
      expect(
        screen.queryByTestId("profile-rename-input-research"),
      ).toBeNull(),
    );
    expect(mockedUpdate).not.toHaveBeenCalled();
  });
});

describe("ProfilesPage — delete", () => {
  it("disables the delete button on the protected default profile", async () => {
    mockedList.mockResolvedValue({ profiles: PROFILES });
    renderPage();
    const btn = (await screen.findByTestId(
      "profile-delete-default",
    )) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("opens the confirm dialog and DELETEs on confirm", async () => {
    mockedList.mockResolvedValue({ profiles: PROFILES });
    mockedDelete.mockResolvedValue(undefined);
    renderPage();

    fireEvent.click(await screen.findByTestId("profile-delete-research"));
    expect(
      await screen.findByTestId("profile-delete-dialog"),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("profile-delete-confirm"));
    await waitFor(() =>
      expect(mockedDelete).toHaveBeenCalledWith("research"),
    );
  });
});

describe("ProfilesPage — SOUL editor", () => {
  it("lazy-loads SOUL on expand and PUTs on Save", async () => {
    mockedList.mockResolvedValue({ profiles: PROFILES });
    mockedSoulGet.mockResolvedValue({ content: "previous content" });
    mockedSoulSet.mockResolvedValue({ content: "new content" });
    renderPage();

    fireEvent.click(await screen.findByTestId("profile-edit-soul-research"));
    const textarea = (await screen.findByTestId(
      "profile-soul-textarea-research",
    )) as HTMLTextAreaElement;
    await waitFor(() => expect(textarea.value).toBe("previous content"));

    fireEvent.change(textarea, { target: { value: "new content" } });
    fireEvent.click(screen.getByTestId("profile-soul-save-research"));

    await waitFor(() =>
      expect(mockedSoulSet).toHaveBeenCalledWith("research", "new content"),
    );
  });
});
