import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config — Wave 5.1.
 *
 * Two `webServer` entries are conditionally enabled when
 * `CORLINMAN_E2E=1` is set. They start:
 *
 *   1. The Python gateway (`corlinman-gateway`) on port 6005. Specs
 *      that mutate state (onboarding, profile lifecycle, curator) need
 *      a real backend — mocking these flows would defeat the purpose
 *      of W5.1 (which is to exercise the whole stack).
 *   2. The Next.js dev server on port 3000.
 *
 * Without `CORLINMAN_E2E=1`, Playwright assumes both services are
 * already running (this is the common local-dev case) and the affected
 * specs `test.skip()` themselves at suite level — keeping `pnpm
 * playwright test` cheap and predictable for contributors who only
 * have the UI dev server up.
 *
 * Env vars consumed:
 *   - `PLAYWRIGHT_BASE_URL`     — UI base (default http://localhost:3000)
 *   - `PLAYWRIGHT_GATEWAY_URL`  — gateway base (default http://localhost:6005)
 *   - `CORLINMAN_E2E=1`         — gate full-stack E2E specs on
 *   - `CI`                      — Playwright's standard CI tweaks
 */

const wantsFullStack = process.env.CORLINMAN_E2E === "1";

const baseURL =
  process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:3000";
const gatewayURL =
  process.env.PLAYWRIGHT_GATEWAY_URL ?? "http://localhost:6005";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  // Default to a generous timeout — full-stack specs need ~30s to spin
  // up the gateway lifecycle on cold start, and the existing spec dir
  // budget was the framework default (30s). Make it explicit.
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],
  use: {
    baseURL,
    trace: "on-first-retry",
    extraHTTPHeaders: {
      // Surfaces the spec name in gateway access logs so failures are
      // easier to triage.
      "x-corlinman-source": "playwright",
    },
  },
  webServer: wantsFullStack
    ? [
        {
          // Gateway first — the UI dev server expects /admin/* to
          // proxy through Next's rewrites once the gateway is alive.
          command: "corlinman-gateway",
          url: `${gatewayURL}/health`,
          reuseExistingServer: !process.env.CI,
          timeout: 60_000,
          env: {
            // Use a throwaway data dir per run so admin/root seed is
            // re-installed cleanly. The path is interpreted by the
            // gateway entrypoint relative to the user's data dir
            // override; without this each run inherits the previous
            // run's rotated password.
            CORLINMAN_DATA_DIR:
              process.env.CORLINMAN_DATA_DIR ?? "/tmp/corlinman-e2e",
          },
        },
        {
          command: "pnpm dev",
          url: baseURL,
          reuseExistingServer: !process.env.CI,
          timeout: 120_000,
        },
      ]
    : undefined,
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
