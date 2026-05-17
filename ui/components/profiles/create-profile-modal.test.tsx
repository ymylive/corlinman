/**
 * Unit tests for the create-profile modal (W3.2).
 *
 * Covers:
 *   - Slug regex validation (invalid: uppercase, empty, starts-with-dash)
 *   - 201 success → onCreated fires + dialog closes + active profile flips
 *   - 409 profile_exists → inline slug error
 *   - 422 invalid_slug → inline slug error (server message extracted)
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

// Mock the profile API surface before the SUT imports it.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    createProfile: vi.fn(),
    listProfiles: vi.fn(),
  };
});

// Sonner toasts: not the focus here; stub to keep stdout clean.
vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

/**
 * jsdom in this repo ships ``window.localStorage`` without the full
 * Storage interface (``.clear`` / ``.getItem`` are missing). Install a
 * minimal in-memory implementation so the active-profile context can
 * round-trip the active slug.
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

// next/navigation isn't needed by the modal itself but the active-profile
// context that the modal calls into doesn't import it either, so no mock
// needed beyond router.push (which we don't exercise here).

import { CorlinmanApiError, createProfile, listProfiles } from "@/lib/api";
import {
  ActiveProfileProvider,
  STORAGE_KEY,
} from "@/lib/context/active-profile";
import {
  CreateProfileModal,
  PROFILE_SLUG_RE,
} from "./create-profile-modal";

const mockedCreate = vi.mocked(createProfile);
const mockedList = vi.mocked(listProfiles);

const FIXTURE_PROFILES = [
  {
    slug: "default",
    display_name: "Default",
    parent_slug: null,
    description: null,
    created_at: "2026-04-01T00:00:00Z",
  },
];

function renderModal(onCreated?: (p: { slug: string }) => void) {
  const onOpenChange = vi.fn();
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  // Seed list ONLY if the test hasn't already configured one. Tests that
  // need to flip the response after creation set their own
  // ``mockImplementation`` *before* calling ``renderModal``.
  const impl = mockedList.getMockImplementation();
  if (!impl) {
    mockedList.mockResolvedValue({ profiles: FIXTURE_PROFILES });
  }
  const utils = render(
    <QueryClientProvider client={qc}>
      <ActiveProfileProvider>
        <CreateProfileModal
          open
          onOpenChange={onOpenChange}
          profiles={FIXTURE_PROFILES}
          onCreated={onCreated}
        />
      </ActiveProfileProvider>
    </QueryClientProvider>,
  );
  return { ...utils, onOpenChange };
}

beforeEach(() => {
  mockedCreate.mockReset();
  mockedList.mockReset();
  installLocalStorageShim();
});

afterEach(() => {
  cleanup();
});

// ---------------------------------------------------------------------------
// Regex sanity
// ---------------------------------------------------------------------------

describe("PROFILE_SLUG_RE", () => {
  it("accepts kebab/snake lowercase slugs", () => {
    expect(PROFILE_SLUG_RE.test("a")).toBe(true);
    expect(PROFILE_SLUG_RE.test("a1")).toBe(true);
    expect(PROFILE_SLUG_RE.test("research-bot")).toBe(true);
    expect(PROFILE_SLUG_RE.test("my_profile_2")).toBe(true);
  });
  it("rejects uppercase / leading-symbol / spaces / too long", () => {
    expect(PROFILE_SLUG_RE.test("Acme")).toBe(false);
    expect(PROFILE_SLUG_RE.test("-leading")).toBe(false);
    expect(PROFILE_SLUG_RE.test("_leading")).toBe(false);
    expect(PROFILE_SLUG_RE.test("has space")).toBe(false);
    expect(PROFILE_SLUG_RE.test("a".repeat(65))).toBe(false);
    expect(PROFILE_SLUG_RE.test("")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Client-side validation
// ---------------------------------------------------------------------------

describe("CreateProfileModal — client validation", () => {
  it("surfaces an inline error as the user types an uppercase slug", async () => {
    renderModal();
    fireEvent.change(screen.getByTestId("profile-slug"), {
      target: { value: "ACME" },
    });
    expect(
      await screen.findByTestId("profile-slug-error"),
    ).toBeInTheDocument();
    // Empty slug erases the error.
    fireEvent.change(screen.getByTestId("profile-slug"), {
      target: { value: "" },
    });
    await waitFor(() =>
      expect(screen.queryByTestId("profile-slug-error")).toBeNull(),
    );
  });

  it("blocks submit when the slug is empty", async () => {
    renderModal();
    fireEvent.click(screen.getByTestId("create-profile-submit"));
    await waitFor(() =>
      expect(screen.getByTestId("profile-slug-error")).toBeInTheDocument(),
    );
    expect(mockedCreate).not.toHaveBeenCalled();
  });

  it("blocks submit on a regex-failing slug", async () => {
    renderModal();
    fireEvent.change(screen.getByTestId("profile-slug"), {
      target: { value: "-bad" },
    });
    fireEvent.click(screen.getByTestId("create-profile-submit"));
    await waitFor(() =>
      expect(screen.getByTestId("profile-slug-error")).toBeInTheDocument(),
    );
    expect(mockedCreate).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Submission flow
// ---------------------------------------------------------------------------

describe("CreateProfileModal — submission", () => {
  it("posts on a valid slug and closes on 201", async () => {
    const created = {
      slug: "research",
      display_name: "research",
      parent_slug: "default",
      description: null,
      created_at: "2026-05-17T00:00:00Z",
    };
    mockedCreate.mockResolvedValueOnce(created);
    // The provider's self-heal effect resets the slug to "default" if the
    // freshly-set slug isn't present in the resolved profile list. Have
    // the post-create refetch include "research" so the self-heal sees
    // the new row.
    mockedList.mockImplementation(async () => {
      if (mockedCreate.mock.calls.length > 0) {
        return { profiles: [...FIXTURE_PROFILES, created] };
      }
      return { profiles: FIXTURE_PROFILES };
    });
    const onCreated = vi.fn();
    const { onOpenChange } = renderModal(onCreated);

    fireEvent.change(screen.getByTestId("profile-slug"), {
      target: { value: "research" },
    });
    fireEvent.click(screen.getByTestId("create-profile-submit"));

    await waitFor(() =>
      expect(mockedCreate).toHaveBeenCalledWith({
        slug: "research",
        display_name: undefined,
        description: undefined,
        clone_from: "default",
      }),
    );
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(created));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    // localStorage gets the new active slug (the modal flips
    // ``useActiveProfile().setSlug`` to the newly-created profile).
    await waitFor(() =>
      expect(window.localStorage.getItem(STORAGE_KEY)).toBe("research"),
    );
  });

  it("renders the slug error inline on a 409", async () => {
    mockedCreate.mockRejectedValueOnce(
      new CorlinmanApiError(
        JSON.stringify({ detail: { error: "profile_exists" } }),
        409,
      ),
    );
    renderModal();
    fireEvent.change(screen.getByTestId("profile-slug"), {
      target: { value: "research" },
    });
    fireEvent.click(screen.getByTestId("create-profile-submit"));
    const err = await screen.findByTestId("profile-slug-error");
    expect(err.textContent ?? "").toMatch(/research/);
  });

  it("renders the server message on a 422", async () => {
    mockedCreate.mockRejectedValueOnce(
      new CorlinmanApiError(
        JSON.stringify({
          detail: { error: "invalid_slug", message: "slug too long" },
        }),
        422,
      ),
    );
    renderModal();
    fireEvent.change(screen.getByTestId("profile-slug"), {
      target: { value: "research" },
    });
    fireEvent.click(screen.getByTestId("create-profile-submit"));
    const err = await screen.findByTestId("profile-slug-error");
    expect(err.textContent ?? "").toMatch(/slug too long/);
  });
});
