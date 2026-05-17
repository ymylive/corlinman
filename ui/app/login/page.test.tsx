/**
 * Login page smoke test. Exercises:
 *   1. The form renders with username + password fields.
 *   2. Submitting with mocked `/admin/login` → router.replace('/').
 *
 * The `fetch` stub returns 200 so `login()` resolves cleanly; that's
 * enough to cover the happy path without pulling in MSW or vi.mock
 * gymnastics.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const replaceMock = vi.fn();
const pushMock = vi.fn();
let searchParams = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: pushMock }),
  useSearchParams: () => searchParams,
  usePathname: () => "/",
}));

import LoginPage from "./page";

describe("LoginPage", () => {
  beforeEach(() => {
    replaceMock.mockClear();
    pushMock.mockClear();
    searchParams = new URLSearchParams();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ token: "t", expires_in: 86400 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders username + password fields", () => {
    render(<LoginPage />);
    expect(screen.getByLabelText("用户名")).toBeInTheDocument();
    expect(screen.getByLabelText("密码")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "登录" })).toBeInTheDocument();
  });

  it("renders decorative shimmer backdrop layers", () => {
    render(<LoginPage />);
    // Both decoration layers live on the hero column. They are aria-hidden
    // and CSS-driven; the reduced-motion branch lives in a @media block so
    // the DOM is stable — assertion is just that the classes are present.
    const dotDrift = document.querySelector(".login-dot-drift");
    const shimmer = document.querySelector(".login-shimmer-glow");
    expect(dotDrift).not.toBeNull();
    expect(shimmer).not.toBeNull();
    // Not using Tailwind `animate-*` utilities — the animation is scoped via
    // a component-local <style> block and disabled via @media CSS.
    expect(dotDrift?.className).not.toMatch(/\banimate-/);
    expect(shimmer?.className).not.toMatch(/\banimate-/);
  });

  it("calls /admin/login and redirects on success", async () => {
    // The Wave 1.4 flow does a follow-up /admin/me probe — return a
    // session that has already rotated so we hit the `/` redirect branch.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.includes("/admin/me")) {
          return new Response(
            JSON.stringify({
              user: "admin",
              created_at: "2026-05-17T00:00:00Z",
              expires_at: "2026-05-24T00:00:00Z",
              must_change_password: false,
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({ token: "t", expires_in: 86400 }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }),
    );

    render(<LoginPage />);
    fireEvent.change(screen.getByLabelText("用户名"), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/"));
    const fetchCalls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock
      .calls;
    expect(fetchCalls[0][0]).toContain("/admin/login");
    expect(fetchCalls[0][1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ username: "admin", password: "secret" }),
    });
  });

  it(
    "ignores ?redirect= and forces /account/security when " +
      "must_change_password is true",
    async () => {
      // Land with a redirect target. The Wave 1.4 contract says we
      // *ignore* this whenever the gateway tells us the seed hasn't
      // been rotated yet.
      searchParams = new URLSearchParams({ redirect: "/agents" });

      vi.stubGlobal(
        "fetch",
        vi.fn(async (url: string) => {
          if (url.includes("/admin/me")) {
            return new Response(
              JSON.stringify({
                user: "admin",
                created_at: "2026-05-17T00:00:00Z",
                expires_at: "2026-05-24T00:00:00Z",
                must_change_password: true,
              }),
              {
                status: 200,
                headers: { "content-type": "application/json" },
              },
            );
          }
          return new Response(
            JSON.stringify({ token: "t", expires_in: 86400 }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }),
      );

      render(<LoginPage />);
      fireEvent.change(screen.getByLabelText("用户名"), {
        target: { value: "admin" },
      });
      fireEvent.change(screen.getByLabelText("密码"), {
        target: { value: "root" },
      });
      fireEvent.click(screen.getByRole("button", { name: "登录" }));

      await waitFor(() =>
        expect(replaceMock).toHaveBeenCalledWith("/account/security"),
      );
      // Importantly, /agents was NEVER navigated to.
      expect(replaceMock).not.toHaveBeenCalledWith("/agents");
    },
  );
});
