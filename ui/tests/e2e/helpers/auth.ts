/**
 * Auth helpers for Wave 5.1 Playwright E2E.
 *
 * These wrap the real `/login` + `/account/security` flows in the
 * deployed UI — no API short-circuits. The goal of W5.1 is to exercise
 * the actual chain (gateway + UI + cookie round-trip), so each helper
 * uses the same affordances a human operator would (Click, fill, wait
 * for the URL to settle).
 *
 * Locator strategy: we target `data-testid` first, falling back to
 * accessible labels when the page only exposes i18n labels (login form).
 * The labels are the English ones — the harness sets the language to
 * `en` via the localStorage `i18nextLng` key in each spec's
 * `test.beforeEach`. That keeps locator strings stable across runs even
 * when the browser locale defaults to `zh-CN`.
 */

import { expect, type Page } from "@playwright/test";

export const DEFAULT_ADMIN_USER = "admin";
export const DEFAULT_ADMIN_PASSWORD = "root";

/**
 * Pin the in-browser i18n language to English so the helpers can target
 * literal label strings. Must be called once per page (BEFORE the first
 * `page.goto`) — we set it in localStorage and the i18next detector
 * picks it up on init.
 */
export async function pinLocaleEn(page: Page): Promise<void> {
  await page.addInitScript(() => {
    try {
      localStorage.setItem("i18nextLng", "en");
    } catch {
      /* swallow — private mode / SSR edge cases. */
    }
  });
}

/**
 * Visit /login, type credentials, submit, and wait for the post-login
 * URL to settle. The default-password flow is observable but not
 * asserted here — callers that care about it should check the URL
 * themselves.
 */
export async function loginAsAdmin(
  page: Page,
  password: string = DEFAULT_ADMIN_PASSWORD,
  username: string = DEFAULT_ADMIN_USER,
): Promise<void> {
  await page.goto("/login");
  // Wait for the form to mount; the Suspense boundary in
  // `login/page.tsx` renders a disabled shell first.
  await expect(page.locator("#username")).toBeVisible();
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(password);
  await Promise.all([
    page.waitForURL((u) => !/\/login(\?|$)/.test(u.pathname)),
    page.getByRole("button", { name: /^Sign in$/i }).click(),
  ]);
}

/**
 * Rotate the password by driving the change-password card on
 * /account/security. The helper navigates there itself so callers can
 * be on any authenticated page when they call it.
 */
export async function rotateAdminPassword(
  page: Page,
  oldPw: string,
  newPw: string,
): Promise<void> {
  if (!/\/account\/security$/.test(new URL(page.url(), "http://x").pathname)) {
    await page.goto("/account/security");
  }
  await page.getByTestId("cpw-old").fill(oldPw);
  await page.getByTestId("cpw-new").fill(newPw);
  await page.getByTestId("cpw-confirm").fill(newPw);
  await page.getByTestId("password-submit").click();
  // The success state surfaces with the post-rotation callout.
  await expect(page.getByTestId("account-security-resolved")).toBeVisible({
    timeout: 5_000,
  });
}

/**
 * Drive the sidebar's logout button and wait for /login.
 */
export async function logout(page: Page): Promise<void> {
  const button = page.getByTestId("logout-button").first();
  await expect(button).toBeVisible({ timeout: 5_000 });
  await Promise.all([
    page.waitForURL((u) => /\/login(\?|$)/.test(u.pathname), {
      timeout: 5_000,
    }),
    button.click(),
  ]);
}

/**
 * Change the username via the corresponding card on /account/security.
 * The current cookie keeps the operator signed in afterwards — the
 * backend re-derives the session identity transparently.
 */
export async function changeUsername(
  page: Page,
  currentPassword: string,
  newUsername: string,
): Promise<void> {
  await page.goto("/account/security");
  await page.getByTestId("username-current-password").fill(currentPassword);
  await page.getByTestId("new-username").fill(newUsername);
  await page.getByTestId("username-submit").click();
  // The success toast (or refresh) is observed by the caller; we just
  // wait for the field to be cleared by the on-success handler.
  await expect(page.getByTestId("new-username")).toHaveValue("", {
    timeout: 5_000,
  });
}
