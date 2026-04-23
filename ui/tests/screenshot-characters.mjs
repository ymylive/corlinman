// Tidepool Phase 5c screenshot helper for the Characters page.
// Drives a Playwright-chromium to capture dark/light renders of the
// offline state (gateway is offline in dev, which is expected for this pass).
//
// Usage: node tests/screenshot-characters.mjs
import { chromium } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";

const OUT_DIR = resolve(process.cwd(), "..", "_design");
mkdirSync(OUT_DIR, { recursive: true });

const targets = [
  { theme: "dark", file: "phase5c-characters-dark.png" },
  { theme: "light", file: "phase5c-characters-light.png" },
];

const browser = await chromium.launch();
try {
  for (const t of targets) {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await ctx.newPage();
    await page.goto(`http://localhost:3000/characters?theme=${t.theme}`, {
      waitUntil: "networkidle",
      timeout: 30_000,
    });
    // Brief settle for aurora/backdrop-filter composition.
    await page.waitForTimeout(600);
    const outPath = resolve(OUT_DIR, t.file);
    await page.screenshot({ path: outPath, fullPage: true });
    // eslint-disable-next-line no-console
    console.log("wrote", outPath);
    await ctx.close();
  }
} finally {
  await browser.close();
}
