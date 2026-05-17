/**
 * Wave 5.1 — onboard → must-change-password → admin → logout → login
 * round trip. Full chain against a real gateway + UI.
 *
 * Running locally:
 *
 *     # First time only — installs the Chromium browser binary.
 *     cd ui && pnpm playwright install chromium
 *
 *     # Boot gateway + UI yourself, then:
 *     cd ui && pnpm playwright test tests/e2e/onboard-to-admin.spec.ts
 *
 *     # OR let Playwright manage both processes (clean data dir):
 *     CORLINMAN_E2E=1 cd ui && pnpm playwright test \
 *         tests/e2e/onboard-to-admin.spec.ts
 *
 * The suite is gated behind `CORLINMAN_E2E=1`. Without it the specs
 * `.skip` so contributors who only have the Vitest unit test runner
 * available aren't blocked.
 *
 * Side effects: this spec rotates the admin credentials. The teardown
 * hooks reset them back to `admin/root` so subsequent specs (and reruns)
 * can rely on the default seed. If the spec aborts mid-flight you may
 * need to wipe `config.toml [admin]` by hand.
 */

import { expect, test } from "@playwright/test";

import {
  changeUsername,
  DEFAULT_ADMIN_PASSWORD,
  DEFAULT_ADMIN_USER,
  loginAsAdmin,
  logout,
  pinLocaleEn,
  rotateAdminPassword,
} from "./helpers/auth";
import {
  expectDefaultAdminSeed,
  resetAdminPassword,
  resetAdminUsername,
} from "./helpers/test-data";

const FULL_STACK = process.env.CORLINMAN_E2E === "1";
const ROTATED_PASSWORD = "newpassword123";
const ROTATED_USERNAME = "ops";

// State accumulates across the serial chain — track the *currently
// believed* credentials so teardown can restore them no matter where
// the spec failed.
let liveUser = DEFAULT_ADMIN_USER;
let livePassword = DEFAULT_ADMIN_PASSWORD;

(FULL_STACK ? test.describe.serial : test.describe.skip)(
  "Wave 5.1 — onboard to admin (full chain)",
  () => {
    test.beforeAll(async ({ request }) => {
      await expectDefaultAdminSeed(request);
    });

    test.afterAll(async ({ request }) => {
      // Best-effort restore: rotate user back to admin, then password
      // back to root. Two-step so we know the gateway accepts the
      // current creds before mutating.
      await resetAdminUsername(request, liveUser, livePassword).catch(
        () => undefined,
      );
      await resetAdminPassword(request, livePassword).catch(() => undefined);
      liveUser = DEFAULT_ADMIN_USER;
      livePassword = DEFAULT_ADMIN_PASSWORD;
    });

    test.beforeEach(async ({ page }) => {
      await pinLocaleEn(page);
    });

    test("1. admin/root login forces /account/security", async ({ page }) => {
      await loginAsAdmin(page);
      await expect(page).toHaveURL(/\/account\/security$/);
      await expect(
        page.getByTestId("default-password-banner"),
      ).toBeVisible();
      // Banner copy literally mentions "default password" — sanity-
      // check the substring so a future i18n refactor that drops this
      // word fails the test instead of silently regressing the warning.
      await expect(
        page.getByTestId("default-password-banner"),
      ).toContainText(/default password/i);
    });

    test("2. rotate password — banner disappears, CTA navigates", async ({
      page,
    }) => {
      await loginAsAdmin(page);
      await expect(page).toHaveURL(/\/account\/security$/);

      await rotateAdminPassword(
        page,
        DEFAULT_ADMIN_PASSWORD,
        ROTATED_PASSWORD,
      );
      livePassword = ROTATED_PASSWORD;

      // Banner disappears after must_change_password flips false.
      await expect(
        page.getByTestId("default-password-banner"),
      ).toBeHidden();

      // The resolved success card surfaces; click "Continue to dashboard".
      await expect(page.getByTestId("account-security-resolved")).toBeVisible();
      await Promise.all([
        page.waitForURL((u) => u.pathname === "/"),
        page
          .getByTestId("account-security-resolved")
          .getByRole("button", { name: /continue to dashboard/i })
          .click(),
      ]);
      await expect(page).toHaveURL(/\/$/);

      // Reload — still authenticated, no force-redirect.
      await page.reload();
      await expect(page).not.toHaveURL(/\/login/);
      await expect(page).not.toHaveURL(/\/account\/security$/);
    });

    test("3. logout invalidates session; old creds rejected", async ({
      page,
    }) => {
      // Re-establish a session for this fresh page; the previous test's
      // browser context is isolated.
      await loginAsAdmin(page, livePassword);
      // Post-rotate the must_change flag is off, so login lands on `/`.
      await expect(page).toHaveURL(/\/$/);

      await logout(page);
      await expect(page).toHaveURL(/\/login(\?|$)/);

      // Old creds (admin/root) should now fail — the password was
      // rotated. The error surfaces via the `login-error` testid.
      await page.locator("#username").fill(DEFAULT_ADMIN_USER);
      await page.locator("#password").fill(DEFAULT_ADMIN_PASSWORD);
      await page.getByRole("button", { name: /^Sign in$/i }).click();
      await expect(page.getByTestId("login-error")).toBeVisible({
        timeout: 5_000,
      });
      // Still on /login — the failed POST shouldn't have navigated us.
      await expect(page).toHaveURL(/\/login(\?|$)/);
    });

    test("4. login with rotated password lands on dashboard", async ({
      page,
    }) => {
      await loginAsAdmin(page, livePassword);
      // must_change_password is false now → no forced redirect.
      await expect(page).toHaveURL(/\/$/);
      await expect(
        page.getByTestId("default-password-banner"),
      ).toHaveCount(0);
    });

    test("5. rename admin → ops, then login as ops", async ({ page }) => {
      await loginAsAdmin(page, livePassword);
      await changeUsername(page, livePassword, ROTATED_USERNAME);
      liveUser = ROTATED_USERNAME;

      // Logout, then sign back in with the new username. The backend
      // re-derives the cookie identity transparently, but driving a
      // full logout+login cycle is what proves the username persisted
      // to TOML (not just the in-memory session).
      await logout(page);
      await loginAsAdmin(page, livePassword, ROTATED_USERNAME);
      await expect(page).toHaveURL(/\/$/);
    });
  },
);
