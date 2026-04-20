import { test, expect } from "@playwright/test";

// Placeholder smoke test. Replaced with real admin flows in M6.
test("home page renders title", async ({ page }) => {
  await page.goto("/");
  // TODO(M6): switch to data-testid once real layout lands.
  await expect(page.locator("body")).toBeVisible();
});
