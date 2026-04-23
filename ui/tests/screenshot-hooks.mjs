// Tidepool Phase 5c screenshot helper for the Hooks page.
// Captures dark/light renders via Playwright-chromium at 1440×900.
//
// Usage: node tests/screenshot-hooks.mjs
import { chromium } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";

const OUT_DIR = resolve(process.cwd(), "..", "_design");
mkdirSync(OUT_DIR, { recursive: true });

const targets = [
  { theme: "dark", file: "phase5c-hooks-dark.png" },
  { theme: "light", file: "phase5c-hooks-light.png" },
];

const browser = await chromium.launch();
try {
  for (const t of targets) {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await ctx.newPage();
    await page.goto(`http://localhost:3000/hooks?theme=${t.theme}`, {
      waitUntil: "networkidle",
      timeout: 30_000,
    });
    // Settle for aurora/backdrop-filter composition + a few stream events.
    await page.waitForTimeout(1800);
    const outPath = resolve(OUT_DIR, t.file);
    await page.screenshot({ path: outPath, fullPage: true });
    // eslint-disable-next-line no-console
    console.log("wrote", outPath);
    await ctx.close();
  }
} finally {
  await browser.close();
}
