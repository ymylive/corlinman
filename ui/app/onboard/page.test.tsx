/**
 * Onboarding page smoke test. Exercises:
 *   1. Renders username + password + confirm fields.
 *   2. Mismatched passwords surface an inline error without hitting the
 *      network.
 *   3. Submitting matching credentials calls POST /admin/onboard and
 *      navigates to /login on success.
 *
 * Tests run under the default zh-CN locale (matching login page tests).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const replaceMock = vi.fn();
const pushMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: pushMock }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/onboard",
}));

import OnboardPage from "./page";

describe("OnboardPage", () => {
  beforeEach(() => {
    replaceMock.mockClear();
    pushMock.mockClear();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ status: "ok" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the three onboarding fields", () => {
    render(<OnboardPage />);
    expect(screen.getByLabelText("用户名")).toBeInTheDocument();
    expect(screen.getByLabelText("密码")).toBeInTheDocument();
    expect(screen.getByLabelText("确认密码")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "创建管理员" }),
    ).toBeInTheDocument();
  });

  it("surfaces an inline mismatch error without calling fetch", async () => {
    render(<OnboardPage />);
    fireEvent.change(screen.getByLabelText("用户名"), {
      target: { value: "alice" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "abcdefgh" },
    });
    fireEvent.change(screen.getByLabelText("确认密码"), {
      target: { value: "different" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建管理员" }));

    await waitFor(() => {
      expect(screen.getByTestId("onboard-error")).toHaveTextContent(
        "两次密码不一致",
      );
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("calls /admin/onboard and advances to the newapi step on success", async () => {
    render(<OnboardPage />);
    fireEvent.change(screen.getByLabelText("用户名"), {
      target: { value: "alice" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "goodpassphrase" },
    });
    fireEvent.change(screen.getByLabelText("确认密码"), {
      target: { value: "goodpassphrase" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建管理员" }));

    // No redirect after step 1 — wizard advances to step 2 (newapi).
    await waitFor(() => {
      expect(screen.getByLabelText("newapi 地址")).toBeInTheDocument();
    });
    expect(replaceMock).not.toHaveBeenCalled();
    const fetchCalls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock
      .calls;
    expect(fetchCalls[0][0]).toContain("/admin/onboard");
    expect(fetchCalls[0][1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ username: "alice", password: "goodpassphrase" }),
    });
  });
});
