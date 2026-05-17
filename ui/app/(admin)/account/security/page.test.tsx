/**
 * /account/security page tests (Wave 1.3 / 1.4).
 *
 * Coverage:
 *   1. Both forms (username + password) render.
 *   2. Submitting an empty new password → inline error, no network call.
 *   3. Mismatched confirm → inline error, no network call.
 *   4. Happy-path rotation → POST /admin/password fires, toast emitted,
 *      `/admin/me` refetched.
 *
 * Locale follows the rest of the suite (zh-CN), so the assertions read
 * Chinese strings — see `vitest.setup.ts` for the bundle init.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const replaceMock = vi.fn();
const pushMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: pushMock }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/account/security",
}));

// Sonner emits side effects we don't actually need to render. Stub it
// so we can assert the call without a real toast container.
// `vi.mock` is hoisted to the top of the file, so the spy refs MUST go
// through `vi.hoisted` — closing over a normal `const` would race the
// import order and throw "Cannot access before initialization".
const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}));
vi.mock("sonner", () => ({
  toast: { success: toastSuccess, error: toastError },
}));

import AccountSecurityPage from "./page";

describe("AccountSecurityPage", () => {
  beforeEach(() => {
    replaceMock.mockClear();
    pushMock.mockClear();
    toastSuccess.mockClear();
    toastError.mockClear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** Per-call response sequencer — drives the mocked fetch. */
  function stubFetch(
    handler: (url: string, init?: RequestInit) => Response | Promise<Response>,
  ) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => handler(url, init)),
    );
  }

  it("renders the change-username + change-password forms", async () => {
    stubFetch(() =>
      new Response(
        JSON.stringify({
          user: "admin",
          created_at: "2026-05-17T00:00:00Z",
          expires_at: "2026-05-24T00:00:00Z",
          must_change_password: true,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );

    render(<AccountSecurityPage />);

    expect(
      await screen.findByTestId("card-change-username"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("card-change-password")).toBeInTheDocument();
    // Both cards have a "current password" field — both labels render.
    expect(screen.getAllByText("当前密码").length).toBeGreaterThanOrEqual(2);
  });

  it("blocks an empty new-password submission with an inline error", async () => {
    stubFetch(() =>
      new Response(
        JSON.stringify({
          user: "admin",
          created_at: "2026-05-17T00:00:00Z",
          expires_at: "2026-05-24T00:00:00Z",
          must_change_password: true,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );

    render(<AccountSecurityPage />);
    await screen.findByTestId("card-change-password");

    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    // Reset call log so we can assert the next click does NOT fire one.
    fetchMock.mockClear();

    fireEvent.change(screen.getByTestId("cpw-old"), {
      target: { value: "root" },
    });
    // newPassword stays empty
    fireEvent.click(screen.getByTestId("password-submit"));

    await waitFor(() => {
      expect(screen.getByTestId("password-error")).toBeInTheDocument();
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("flags mismatched new + confirm without hitting the network", async () => {
    stubFetch(() =>
      new Response(
        JSON.stringify({
          user: "admin",
          created_at: "2026-05-17T00:00:00Z",
          expires_at: "2026-05-24T00:00:00Z",
          must_change_password: true,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );

    render(<AccountSecurityPage />);
    await screen.findByTestId("card-change-password");

    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    fetchMock.mockClear();

    fireEvent.change(screen.getByTestId("cpw-old"), {
      target: { value: "root" },
    });
    fireEvent.change(screen.getByTestId("cpw-new"), {
      target: { value: "newpassphrase" },
    });
    fireEvent.change(screen.getByTestId("cpw-confirm"), {
      target: { value: "something-else" },
    });
    fireEvent.click(screen.getByTestId("password-submit"));

    await waitFor(() => {
      expect(screen.getByTestId("password-error")).toHaveTextContent(
        "两次输入不一致",
      );
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("calls /admin/password on success and refetches /admin/me", async () => {
    let meCalls = 0;
    let passwordCalls = 0;
    stubFetch((url) => {
      if (url.includes("/admin/me")) {
        meCalls++;
        // First call (mount) → must_change_password=true; subsequent
        // calls (post-rotate) → false so the success notice renders.
        return new Response(
          JSON.stringify({
            user: "admin",
            created_at: "2026-05-17T00:00:00Z",
            expires_at: "2026-05-24T00:00:00Z",
            must_change_password: meCalls === 1,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/admin/password")) {
        passwordCalls++;
        return new Response(JSON.stringify({ status: "ok" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response("", { status: 404 });
    });

    render(<AccountSecurityPage />);
    await screen.findByTestId("card-change-password");
    // Wait for the mount-time /admin/me to settle.
    await waitFor(() => expect(meCalls).toBeGreaterThanOrEqual(1));

    fireEvent.change(screen.getByTestId("cpw-old"), {
      target: { value: "root" },
    });
    fireEvent.change(screen.getByTestId("cpw-new"), {
      target: { value: "brand_new_pass" },
    });
    fireEvent.change(screen.getByTestId("cpw-confirm"), {
      target: { value: "brand_new_pass" },
    });
    fireEvent.click(screen.getByTestId("password-submit"));

    await waitFor(() => expect(passwordCalls).toBe(1));
    await waitFor(() => expect(meCalls).toBeGreaterThanOrEqual(2));

    expect(toastSuccess).toHaveBeenCalledWith("密码已更新");
    // Success notice + "continue to dashboard" CTA render after refetch.
    await waitFor(() => {
      expect(screen.getByTestId("account-security-resolved")).toBeInTheDocument();
    });
  });
});
