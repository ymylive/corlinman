/**
 * Onboarding page tests (Wave 2.1 reshape).
 *
 * Existing behavior — still covered:
 *   1. When `/admin/me` returns 401 (no admin yet) the wizard starts at
 *      Step 1 (account) with three fields.
 *   2. Mismatched passwords surface an inline error without hitting the
 *      onboard endpoint.
 *   3. Submitting matching credentials calls POST /admin/onboard and
 *      advances to Step 2 (newapi).
 *
 * Wave 2.1 additions:
 *   4. When `/admin/me` returns 200 with `must_change_password=true` the
 *      wizard skips Step 1, lands on Step 2, and renders the
 *      "Using default admin/root" hint.
 *   5. The Step-2 skip button POSTs `/admin/onboard/finalize-skip` and
 *      renders the success card with the mock-provider variant.
 *   6. Smoke-test for the ModelPickerDialog integration: opening the LLM
 *      picker and selecting a model populates the row preview.
 *
 * Locale stays zh-CN (matches login + account/security suites).
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

/**
 * Build a fetch stub that lets each test wire up the route table.
 * Returns 404 by default so unexpected calls are obvious in failures.
 */
function stubFetch(
  handler: (url: string, init?: RequestInit) => Response | Promise<Response>,
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => handler(url, init)),
  );
}

/** Most tests want a default unauth `/admin/me` reply. */
function unauthMeHandler(): Response {
  return new Response(JSON.stringify({ detail: "unauthorized" }), {
    status: 401,
    headers: { "content-type": "application/json" },
  });
}

describe("OnboardPage", () => {
  beforeEach(() => {
    replaceMock.mockClear();
    pushMock.mockClear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("starts at Step 1 when /admin/me returns 401 (no admin yet)", async () => {
    stubFetch((url) => {
      if (url.includes("/admin/me")) return unauthMeHandler();
      return new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });

    render(<OnboardPage />);
    // Wait for the /admin/me probe to settle so the wizard renders the
    // resolved step (not the optimistic "account" mount).
    await waitFor(() => {
      expect(screen.getByTestId("onboard-me-checked")).toHaveAttribute(
        "data-checked",
        "true",
      );
    });

    expect(screen.getByLabelText("用户名")).toBeInTheDocument();
    expect(screen.getByLabelText("密码")).toBeInTheDocument();
    expect(screen.getByLabelText("确认密码")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "创建管理员" }),
    ).toBeInTheDocument();
  });

  it("surfaces an inline mismatch error without calling onboard", async () => {
    stubFetch((url) => {
      if (url.includes("/admin/me")) return unauthMeHandler();
      return new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });

    render(<OnboardPage />);
    await waitFor(() => {
      expect(screen.getByLabelText("用户名")).toBeInTheDocument();
    });

    // Clear the call log so we can assert no /admin/onboard POST happens.
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockClear();

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
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    // The mount-time /admin/me is the only fetch — onboard POST never fires.
    for (const call of fetchMock.mock.calls) {
      expect(String(call[0])).not.toContain("/admin/onboard");
    }
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("calls /admin/onboard and advances to the newapi step on success", async () => {
    stubFetch((url) => {
      if (url.includes("/admin/me")) return unauthMeHandler();
      return new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });

    render(<OnboardPage />);
    await waitFor(() => {
      expect(screen.getByLabelText("用户名")).toBeInTheDocument();
    });
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    fetchMock.mockClear();

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
    const onboardCall = fetchMock.mock.calls.find((c) =>
      String(c[0]).endsWith("/admin/onboard"),
    );
    expect(onboardCall).toBeTruthy();
    expect(onboardCall![1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ username: "alice", password: "goodpassphrase" }),
    });
  });

  it("skips Step 1 + shows the default-admin hint when must_change_password=true", async () => {
    stubFetch((url) => {
      if (url.includes("/admin/me")) {
        return new Response(
          JSON.stringify({
            user: "admin",
            created_at: "2026-05-17T00:00:00Z",
            expires_at: "2026-05-24T00:00:00Z",
            must_change_password: true,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response("", { status: 404 });
    });

    render(<OnboardPage />);

    // The hint only appears once the /admin/me probe resolves.
    await waitFor(() => {
      expect(
        screen.getByTestId("onboard-default-admin-hint"),
      ).toBeInTheDocument();
    });
    // We should be on Step 2 (newapi), not Step 1 (account).
    expect(screen.getByLabelText("newapi 地址")).toBeInTheDocument();
    expect(screen.queryByLabelText("确认密码")).toBeNull();
    // And the "Customize admin account" escape hatch is reachable.
    expect(
      screen.getByTestId("onboard-customize-admin"),
    ).toBeInTheDocument();
  });

  it("clicking Skip → mock provider hits finalize-skip + renders success card", async () => {
    let skipCalls = 0;
    stubFetch((url) => {
      if (url.includes("/admin/me")) {
        return new Response(
          JSON.stringify({
            user: "admin",
            created_at: "2026-05-17T00:00:00Z",
            expires_at: "2026-05-24T00:00:00Z",
            must_change_password: true,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/admin/onboard/finalize-skip")) {
        skipCalls++;
        return new Response(
          JSON.stringify({ status: "ok", mode: "mock" }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response("", { status: 404 });
    });

    render(<OnboardPage />);
    await waitFor(() => {
      expect(screen.getByTestId("onboard-skip-mock")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("onboard-skip-mock"));

    await waitFor(() => {
      expect(skipCalls).toBe(1);
    });
    expect(
      await screen.findByTestId("onboard-success-card"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("onboard-success-subtitle"),
    ).toHaveTextContent("mock provider");
    // Primary CTA points at the security page per the plan.
    expect(
      screen.getByTestId("onboard-cta-security"),
    ).toBeInTheDocument();
  });

  it("Step 3 ModelPickerDialog opens, filters, and applies the picked model", async () => {
    stubFetch((url, init) => {
      if (url.includes("/admin/me")) {
        return new Response(
          JSON.stringify({
            user: "admin",
            created_at: "2026-05-17T00:00:00Z",
            expires_at: "2026-05-24T00:00:00Z",
            must_change_password: true,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/admin/onboard/newapi/probe")) {
        return new Response(
          JSON.stringify({
            base_url: "http://localhost:3000",
            user: { id: 1, username: "admin", role: 1, status: 1 },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/admin/onboard/newapi/channels")) {
        const body = init?.body ? JSON.parse(String(init.body)) : {};
        if (body.type === "llm") {
          return new Response(
            JSON.stringify({
              channels: [
                {
                  id: 1,
                  name: "OpenAI",
                  type: 1,
                  status: 1,
                  models: "gpt-4o, gpt-4o-mini",
                },
              ],
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({ channels: [] }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response("", { status: 404 });
    });

    render(<OnboardPage />);
    await waitFor(() => {
      expect(screen.getByLabelText("newapi 地址")).toBeInTheDocument();
    });

    // Fill out + submit Step 2 to advance into Step 3.
    fireEvent.change(screen.getByLabelText("newapi 地址"), {
      target: { value: "http://localhost:3000" },
    });
    fireEvent.change(screen.getByLabelText("用户令牌 (sk-…)"), {
      target: { value: "sk-test" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "下一步" }),
    );

    // Step 3 mounts; wait until channels finish loading so the edit
    // button is enabled before we click.
    const llmEdit = await screen.findByTestId("model-row-edit-llm");
    await waitFor(() => {
      expect(llmEdit).not.toBeDisabled();
    });
    fireEvent.click(llmEdit);

    // The two-stage dialog renders the OpenAI provider.
    const providerRow = await screen.findByTestId(
      "model-picker-provider-1",
      undefined,
      { timeout: 3000 },
    );
    fireEvent.click(providerRow);

    // Stage 2 — pick gpt-4o-mini.
    const modelRow = await screen.findByTestId(
      "model-picker-model-gpt-4o-mini",
    );
    fireEvent.click(modelRow);

    // Row preview now shows the picked model.
    await waitFor(() => {
      expect(
        screen.getByText("当前选择：gpt-4o-mini"),
      ).toBeInTheDocument();
    });
  });
});
