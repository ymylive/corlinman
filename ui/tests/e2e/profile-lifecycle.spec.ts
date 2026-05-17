/**
 * Wave 5.1 — profile lifecycle E2E.
 *
 * Exercises the `/profiles` admin surface end-to-end:
 *
 *   - create / clone via the modal
 *   - inline rename
 *   - SOUL.md edit + persistence across reload
 *   - profile switcher (top nav) → localStorage round-trip
 *   - delete confirmation flow
 *   - "default" profile is protected (delete disabled)
 *
 * The spec ASSUMES the admin/root seed has been rotated to a known
 * password — but to keep specs independent we don't share state with
 * `onboard-to-admin.spec.ts`. Instead we accept either password (try
 * default first, then the rotated one used by the onboard spec) so
 * the suite is order-independent in the CI matrix.
 *
 * Like spec 1, this is gated behind `CORLINMAN_E2E=1`.
 */

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import {
  DEFAULT_ADMIN_PASSWORD,
  DEFAULT_ADMIN_USER,
  loginAsAdmin,
  pinLocaleEn,
} from "./helpers/auth";
import {
  apiLogin,
  apiLogout,
  apiPurgeTestProfiles,
} from "./helpers/test-data";

const FULL_STACK = process.env.CORLINMAN_E2E === "1";
const TEST_PREFIX = "research-bot";
const ALT_PREFIX = "bad-slug-check";

/** Try every credential we know about and return the one that worked. */
async function discoverPassword(
  request: APIRequestContext,
): Promise<string> {
  for (const candidate of [DEFAULT_ADMIN_PASSWORD, "newpassword123"]) {
    try {
      await apiLogin(request, DEFAULT_ADMIN_USER, candidate);
      await apiLogout(request);
      return candidate;
    } catch {
      /* try the next */
    }
  }
  throw new Error(
    "No known admin password matches. Wipe config.toml [admin] and rerun.",
  );
}

(FULL_STACK ? test.describe.serial : test.describe.skip)(
  "Wave 5.1 — profile lifecycle",
  () => {
    let adminPassword: string;

    test.beforeAll(async ({ request }) => {
      adminPassword = await discoverPassword(request);
      // Clean any leftovers from a prior failed run.
      await apiPurgeTestProfiles(request, [TEST_PREFIX, ALT_PREFIX]);
    });

    test.afterAll(async ({ request }) => {
      await apiPurgeTestProfiles(request, [TEST_PREFIX, ALT_PREFIX]);
    });

    test.beforeEach(async ({ page }) => {
      await pinLocaleEn(page);
      await loginAsAdmin(page, adminPassword);
    });

    test("create / rename / SOUL / clone / delete / protected", async ({
      page,
    }) => {
      // ── 1. List shows `default`, count = 1 (or more, but >= 1) ──
      await page.goto("/profiles");
      const defaultRow = page.getByTestId("profile-row-default");
      await expect(defaultRow).toBeVisible({ timeout: 10_000 });
      await expect(page.getByTestId("profiles-count")).toBeVisible();

      // ── 2. Click "Create profile" → modal opens ──
      await page.getByTestId("profiles-add-btn").click();
      const modal = page.getByTestId("create-profile-form");
      await expect(modal).toBeVisible();

      // ── 3. Invalid slug `Bad-Slug` shows inline error ──
      const slugInput = page.getByTestId("profile-slug");
      await slugInput.fill("Bad-Slug");
      await expect(page.getByTestId("profile-slug-error")).toBeVisible();
      // Per W3.2 the submit button stays enabled (validation surfaces
      // on submit), but clicking it should re-flag the error and NOT
      // navigate / close the modal. If the implementation tightens to
      // disable the submit, both branches are fine.
      const submit = page.getByTestId("create-profile-submit");
      const disabled = await submit.isDisabled().catch(() => false);
      if (!disabled) {
        await submit.click();
        await expect(modal).toBeVisible(); // still open
      }

      // ── 4. Valid slug enables submit ──
      await slugInput.fill("");
      await slugInput.fill(TEST_PREFIX);
      await expect(page.getByTestId("profile-slug-error")).toHaveCount(0);
      await submit.click();

      // ── 5. Modal closes, profile list refreshes ──
      await expect(modal).toBeHidden({ timeout: 5_000 });
      await expect(
        page.getByTestId(`profile-row-${TEST_PREFIX}`),
      ).toBeVisible({ timeout: 5_000 });

      // ── 6. Inline rename via pencil → display_name updates ──
      await page.getByTestId(`profile-rename-${TEST_PREFIX}`).click();
      const renameInput = page.getByTestId(
        `profile-rename-input-${TEST_PREFIX}`,
      );
      await expect(renameInput).toBeVisible();
      await renameInput.fill("Research Bot");
      await renameInput.press("Enter");
      await expect(
        page.getByTestId(`profile-display-name-${TEST_PREFIX}`),
      ).toHaveText(/Research Bot/, { timeout: 5_000 });

      // ── 7. Edit SOUL → textarea → Save → toast ──
      const soulContent =
        "# Research Bot\n\nYou are a research assistant.\n";
      await page.getByTestId(`profile-edit-soul-${TEST_PREFIX}`).click();
      const soul = page.getByTestId(`profile-soul-textarea-${TEST_PREFIX}`);
      await expect(soul).toBeVisible();
      await soul.fill(soulContent);
      await page.getByTestId(`profile-soul-save-${TEST_PREFIX}`).click();

      // ── 8. Reload → SOUL persists ──
      await page.reload();
      await page.getByTestId(`profile-edit-soul-${TEST_PREFIX}`).click();
      const soulAfterReload = page.getByTestId(
        `profile-soul-textarea-${TEST_PREFIX}`,
      );
      await expect(soulAfterReload).toBeVisible();
      // The textarea should have re-hydrated from /admin/profiles/{slug}/soul.
      await expect(soulAfterReload).toHaveValue(soulContent, {
        timeout: 5_000,
      });

      // ── 9. ProfileSwitcher (top nav) on md+ — open & switch ──
      const viewport = page.viewportSize();
      const isDesktop = !!viewport && viewport.width >= 768;
      if (isDesktop) {
        const trigger = page.getByTestId("profile-switcher-trigger");
        await expect(trigger).toBeVisible({ timeout: 5_000 });
        await trigger.click();
        const item = page.getByTestId(`profile-switcher-item-${TEST_PREFIX}`);
        await expect(item).toBeVisible();
        await item.click();
        // localStorage updates
        const stored = await page.evaluate(() =>
          localStorage.getItem("corlinman_active_profile"),
        );
        expect(stored).toContain(TEST_PREFIX);
        // Reload preserves
        await page.reload();
        const stored2 = await page.evaluate(() =>
          localStorage.getItem("corlinman_active_profile"),
        );
        expect(stored2).toContain(TEST_PREFIX);
      }

      // ── 10. Create cloned profile via modal (clone_from set) ──
      const clonedSlug = `${TEST_PREFIX}-2`;
      await page.goto("/profiles");
      await page.getByTestId("profiles-add-btn").click();
      await page.getByTestId("profile-slug").fill(clonedSlug);
      // Select clone_from = TEST_PREFIX via the native <select>.
      await page
        .getByTestId("profile-clone-from")
        .selectOption({ value: TEST_PREFIX });
      await page.getByTestId("create-profile-submit").click();
      await expect(
        page.getByTestId(`profile-row-${clonedSlug}`),
      ).toBeVisible({ timeout: 5_000 });

      // ── 11. Delete the clone → confirm dialog → gone ──
      await page.getByTestId(`profile-delete-${clonedSlug}`).click();
      const dialog = page.getByTestId("profile-delete-dialog");
      await expect(dialog).toBeVisible();
      await page.getByTestId("profile-delete-confirm").click();
      await expect(dialog).toBeHidden({ timeout: 5_000 });
      await expect(
        page.getByTestId(`profile-row-${clonedSlug}`),
      ).toHaveCount(0);

      // ── 12. `default` is protected — delete button disabled ──
      const defaultDeleteBtn = page.getByTestId("profile-delete-default");
      await expect(defaultDeleteBtn).toBeDisabled();
      // Tooltip text is rendered as title= and aria-label= on the
      // button — sniff for the protected copy.
      const tooltip =
        (await defaultDeleteBtn.getAttribute("title")) ??
        (await defaultDeleteBtn.getAttribute("aria-label")) ??
        "";
      expect(tooltip.toLowerCase()).toMatch(/(protect|cannot|default)/);

      // ── Cleanup the deliberately-kept research-bot row ──
      await apiPurgeTestProfilesViaUI(page);
    });
  },
);

/**
 * Best-effort cleanup via the UI. Falls back silently if the API
 * helpers in `afterAll` will take care of it anyway.
 */
async function apiPurgeTestProfilesViaUI(page: Page): Promise<void> {
  try {
    await page.goto("/profiles");
    const row = page.getByTestId(`profile-row-${TEST_PREFIX}`);
    if (await row.count()) {
      await page.getByTestId(`profile-delete-${TEST_PREFIX}`).click();
      await page.getByTestId("profile-delete-confirm").click();
    }
  } catch {
    /* afterAll() will mop up. */
  }
}
