/**
 * Change-password dialog smoke test. Exercises:
 *   1. Renders all three password fields when opened.
 *   2. Submitting with mismatched new+confirm shows an inline error
 *      and never hits the network.
 *   3. Submitting valid input calls POST /admin/password and closes the
 *      dialog (via onOpenChange(false)) on success.
 *   4. 401 from the gateway leaves the dialog open with the
 *      "current password is incorrect" message.
 *
 * Default locale is zh-CN (matches existing UI test conventions).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// Sonner is fire-and-forget here; stub it so the test doesn't try to
// portal a real toast container.
vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

import { ChangePasswordDialog } from "./change-password-dialog";

describe("ChangePasswordDialog", () => {
  const onOpenChange = vi.fn();

  beforeEach(() => {
    onOpenChange.mockClear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function mockFetch(response: { status: number; body?: string }) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(response.body ?? "", {
          status: response.status,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
  }

  it("renders three password fields when open", () => {
    mockFetch({ status: 200, body: JSON.stringify({ status: "ok" }) });
    render(
      <ChangePasswordDialog open onOpenChange={onOpenChange} />,
    );
    expect(screen.getByLabelText("当前密码")).toBeInTheDocument();
    expect(screen.getByLabelText("新密码")).toBeInTheDocument();
    expect(screen.getByLabelText("确认新密码")).toBeInTheDocument();
  });

  it("blocks submit when the two new-password fields disagree", async () => {
    mockFetch({ status: 200, body: JSON.stringify({ status: "ok" }) });
    render(
      <ChangePasswordDialog open onOpenChange={onOpenChange} />,
    );
    fireEvent.change(screen.getByLabelText("当前密码"), {
      target: { value: "oldpass" },
    });
    fireEvent.change(screen.getByLabelText("新密码"), {
      target: { value: "goodpassphrase" },
    });
    fireEvent.change(screen.getByLabelText("确认新密码"), {
      target: { value: "different" },
    });
    fireEvent.click(screen.getByRole("button", { name: "更新密码" }));

    await waitFor(() => {
      expect(screen.getByTestId("change-password-error")).toHaveTextContent(
        "两次新密码不一致",
      );
    });
    expect(globalThis.fetch).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalled();
  });

  it("calls /admin/password and closes on success", async () => {
    mockFetch({ status: 200, body: JSON.stringify({ status: "ok" }) });
    render(
      <ChangePasswordDialog open onOpenChange={onOpenChange} />,
    );
    fireEvent.change(screen.getByLabelText("当前密码"), {
      target: { value: "oldpass" },
    });
    fireEvent.change(screen.getByLabelText("新密码"), {
      target: { value: "brand_new_pass" },
    });
    fireEvent.change(screen.getByLabelText("确认新密码"), {
      target: { value: "brand_new_pass" },
    });
    fireEvent.click(screen.getByRole("button", { name: "更新密码" }));

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    const fetchCalls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock
      .calls;
    expect(fetchCalls[0][0]).toContain("/admin/password");
    expect(fetchCalls[0][1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({
        old_password: "oldpass",
        new_password: "brand_new_pass",
      }),
    });
  });

  it("surfaces 'current password incorrect' on 401 and stays open", async () => {
    mockFetch({
      status: 401,
      body: JSON.stringify({ error: "invalid_old_password" }),
    });
    render(
      <ChangePasswordDialog open onOpenChange={onOpenChange} />,
    );
    fireEvent.change(screen.getByLabelText("当前密码"), {
      target: { value: "WRONG" },
    });
    fireEvent.change(screen.getByLabelText("新密码"), {
      target: { value: "brand_new_pass" },
    });
    fireEvent.change(screen.getByLabelText("确认新密码"), {
      target: { value: "brand_new_pass" },
    });
    fireEvent.click(screen.getByRole("button", { name: "更新密码" }));

    await waitFor(() => {
      expect(screen.getByTestId("change-password-error")).toHaveTextContent(
        "当前密码不正确",
      );
    });
    // Dialog stays open: onOpenChange(false) NOT called.
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });
});
