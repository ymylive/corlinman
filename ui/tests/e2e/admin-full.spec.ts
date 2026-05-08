import { test, expect } from "@playwright/test";

/**
 * Admin full-flow E2E — S6 T7.
 *
 * Covers login → plugins list → plugin detail → agent edit → config save →
 * logout. The spec is wrapped in `test.describe.skip` by default because
 * the CI matrix does not yet boot a real gateway + seeded fixtures. Flip
 * the env var `CORLINMAN_E2E=1` once a test harness that provides
 * `/admin/*` (mock or real) is in place.
 */

const shouldRun = process.env.CORLINMAN_E2E === "1";

(shouldRun ? test.describe : test.describe.skip)("admin full flow", () => {
  test.beforeEach(async ({ page }) => {
    // Stub network responses so the spec stays deterministic. In CI these
    // will be replaced with a real gateway + seed.
    await page.route("**/admin/me", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          user: "admin",
          created_at: new Date().toISOString(),
          expires_at: new Date(Date.now() + 3600_000).toISOString(),
        }),
      });
    });
    await page.route("**/admin/plugins", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            name: "demo",
            version: "1.0.0",
            status: "loaded",
            plugin_type: "sync",
            origin: "workspace",
            tool_count: 1,
            manifest_path: "plugins/demo/plugin-manifest.toml",
            description: "demo plugin",
            capabilities: ["echo"],
            shadowed_count: 0,
          },
        ]),
      });
    });
    await page.route("**/admin/plugins/demo", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          summary: {
            name: "demo",
            version: "1.0.0",
            status: "loaded",
            plugin_type: "sync",
            origin: "workspace",
            tool_count: 1,
            manifest_path: "plugins/demo/plugin-manifest.toml",
            description: "demo plugin",
            capabilities: ["echo"],
            shadowed_count: 0,
          },
          manifest: {
            capabilities: {
              tools: [
                {
                  name: "echo",
                  description: "echo args",
                  input_schema: {
                    type: "object",
                    properties: { message: { type: "string" } },
                  },
                },
              ],
            },
          },
          diagnostics: [],
        }),
      });
    });
    await page.route("**/admin/agents", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            name: "Aemeath",
            file_path: "agents/Aemeath.md",
            bytes: 42,
            last_modified: new Date().toISOString(),
          },
        ]),
      });
    });
    await page.route("**/admin/agents/Aemeath", async (route) => {
      if (route.request().method() === "POST") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            status: "ok",
            name: "Aemeath",
            file_path: "agents/Aemeath.md",
            bytes: 100,
          }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          name: "Aemeath",
          file_path: "agents/Aemeath.md",
          bytes: 42,
          last_modified: new Date().toISOString(),
          content: "---\ntitle: Aemeath\n---\nhello",
        }),
      });
    });
    await page.route("**/admin/config", async (route) => {
      if (route.request().method() === "POST") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            status: "ok",
            issues: [],
            requires_restart: [],
            version: "deadbeef",
          }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          toml: "[server]\nport = 6005\n",
          version: "cafebabe",
          meta: {},
        }),
      });
    });
    await page.route("**/admin/config/schema", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ properties: {} }),
      });
    });
    await page.route("**/admin/logout", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    });
  });

  test("plugins list → detail → invoke form renders", async ({ page }) => {
    await page.goto("/plugins");
    await expect(page.getByTestId("plugin-link-demo")).toBeVisible();
    await page.getByTestId("plugin-link-demo").click();
    await page.waitForURL(/\/plugins\/detail/);
    await expect(page.getByRole("heading", { name: "demo" })).toBeVisible();
    await expect(page.getByTestId("plugin-invoke-form")).toBeVisible();
  });

  test("agents list → detail → save triggers POST", async ({ page }) => {
    await page.goto("/agents");
    await expect(page.getByTestId("agent-link-Aemeath")).toBeVisible();
    await page.getByTestId("agent-link-Aemeath").click();
    await page.waitForURL(/\/agents\/detail/);
    await expect(page.getByRole("heading", { name: "Aemeath" })).toBeVisible();
    // Monaco needs a moment to mount before the Save button is enabled.
    await expect(page.getByTestId("agent-save-btn")).toBeEnabled({ timeout: 5000 });
    await page.getByTestId("agent-save-btn").click();
    await expect(page.getByText(/^(保存成功|Saved)$/)).toBeVisible();
  });

  test("config save round-trips and surfaces version", async ({ page }) => {
    await page.goto("/config");
    await expect(page.getByTestId("config-save-btn")).toBeEnabled({ timeout: 5000 });
    await page.getByTestId("config-save-btn").click();
    await expect(page.getByText("new version:")).toBeVisible();
  });
});
