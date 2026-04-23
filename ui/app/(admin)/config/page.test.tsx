/**
 * Config page micro-effects tests (B3-FE4).
 *
 * Covers the three delight hooks layered on top of the existing Monaco +
 * ArcSwap save flow:
 *   1. Save-success fires `toast.success` with the i18n title/description.
 *   2. Reduced-motion disables the ripple and renders a static <CheckCircle>
 *      inside the toast icon slot.
 *   3. The save button is disabled while the mutation is in-flight.
 *
 * The Monaco editor is mocked out (it's not SSR-safe and offers no value
 * here), and the api module is mocked via `vi.mock` so we control the
 * mutation lifecycle deterministically.
 */

import * as React from "react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CommandPaletteProvider } from "@/components/cmdk-palette";

// next/navigation — cmdk-palette (used transitively by the retokened hero
// ⌘K button) calls useRouter + usePathname + useSearchParams at mount.
vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    refresh: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    prefetch: vi.fn(),
  }),
  usePathname: () => "/config",
  useSearchParams: () => new URLSearchParams(),
  useParams: () => ({}),
  redirect: vi.fn(),
}));

// --- module mocks (hoisted above component import) -------------------------

vi.mock("@/lib/api", () => ({
  fetchConfig: vi.fn(),
  fetchConfigSchema: vi.fn(),
  postConfig: vi.fn(),
}));

vi.mock("next/dynamic", () => ({
  // Strip the Monaco editor — we don't exercise it in these unit tests.
  default: () => {
    const Stub = () => <div data-testid="editor-stub" />;
    return Stub;
  },
}));

const toastSuccessMock = vi.fn();
vi.mock("sonner", () => ({
  toast: {
    success: (...args: unknown[]) => toastSuccessMock(...args),
  },
}));

import {
  fetchConfig,
  fetchConfigSchema,
  postConfig,
  type ConfigGetResponse,
  type ConfigPostResponse,
} from "@/lib/api";
import ConfigPage from "./page";

const mockedFetchConfig = vi.mocked(fetchConfig);
const mockedFetchSchema = vi.mocked(fetchConfigSchema);
const mockedPostConfig = vi.mocked(postConfig);

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <CommandPaletteProvider>
        <ConfigPage />
      </CommandPaletteProvider>
    </QueryClientProvider>,
  );
}

const GET_OK: ConfigGetResponse = {
  toml: '[server]\nport = 8080\n',
  version: "v1",
  meta: {},
};
const POST_OK: ConfigPostResponse = {
  status: "ok",
  issues: [],
  requires_restart: [],
  version: "v2",
};

/** Toggle `(prefers-reduced-motion: reduce)` in jsdom. */
function mockReducedMotion(reduced: boolean) {
  const impl = vi.fn((query: string) => ({
    matches: query === "(prefers-reduced-motion: reduce)" ? reduced : false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }));
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: impl,
  });
}

describe("ConfigPage micro-effects", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    toastSuccessMock.mockReset();
    mockReducedMotion(false);
    mockedFetchConfig.mockResolvedValue(GET_OK);
    mockedFetchSchema.mockResolvedValue({});
  });

  afterEach(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    delete (window as any).matchMedia;
  });

  it("fires toast.success after a successful save", async () => {
    mockedPostConfig.mockResolvedValue(POST_OK);
    renderPage();

    const saveBtn = (await screen.findByTestId(
      "config-save-btn",
    )) as HTMLButtonElement;
    // Wait for the initial config GET to land and un-disable the button.
    await waitFor(() => expect(saveBtn.disabled).toBe(false));

    fireEvent.click(saveBtn);

    await waitFor(() => expect(toastSuccessMock).toHaveBeenCalledTimes(1));
    const [title, opts] = toastSuccessMock.mock.calls[0] as [
      string,
      { description?: string; icon?: React.ReactNode },
    ];
    expect(title).toBe("配置已保存");
    expect(opts.description).toBe("支持热更新的配置已立即生效。");
    expect(opts.icon).toBeTruthy();
  });

  it("under reduced-motion skips the ripple and renders a static checkmark", async () => {
    mockReducedMotion(true);
    mockedPostConfig.mockResolvedValue(POST_OK);
    renderPage();

    const saveBtn = (await screen.findByTestId(
      "config-save-btn",
    )) as HTMLButtonElement;
    await waitFor(() => expect(saveBtn.disabled).toBe(false));

    fireEvent.click(saveBtn);

    await waitFor(() => expect(toastSuccessMock).toHaveBeenCalled());

    // No ripple should ever be in the DOM.
    expect(screen.queryByTestId("config-save-ripple")).toBeNull();

    // The icon passed to toast.success is the reduced-motion <ToastBurst>
    // fallback. Render it standalone and confirm it's the static checkmark.
    const opts = toastSuccessMock.mock.calls[0][1] as { icon?: React.ReactNode };
    expect(opts.icon).toBeTruthy();
    render(<>{opts.icon}</>);
    expect(screen.getByTestId("config-toast-check")).toBeInTheDocument();
    expect(screen.queryByTestId("config-toast-burst")).toBeNull();
  });

  it("disables the Save button while the save request is in-flight", async () => {
    let resolvePost!: (r: ConfigPostResponse) => void;
    mockedPostConfig.mockImplementation(
      () =>
        new Promise<ConfigPostResponse>((res) => {
          resolvePost = res;
        }),
    );
    renderPage();

    const saveBtn = (await screen.findByTestId(
      "config-save-btn",
    )) as HTMLButtonElement;
    await waitFor(() => expect(saveBtn.disabled).toBe(false));

    fireEvent.click(saveBtn);

    // In-flight: button flips to disabled + "Saving…".
    await waitFor(() => expect(saveBtn.disabled).toBe(true));
    expect(saveBtn.textContent).toBe("Saving…");

    // Resolve the mutation — button re-enables.
    resolvePost(POST_OK);
    await waitFor(() => expect(saveBtn.disabled).toBe(false));
  });
});
