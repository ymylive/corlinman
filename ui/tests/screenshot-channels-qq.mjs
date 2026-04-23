// Tidepool Phase 5e screenshot helper for the Channels QQ page.
// Captures dark/light renders against the running dev server. The gateway
// is expected to be offline in dev → the offline block renders; both
// themes should still read as warm glass.
//
// Usage: node tests/screenshot-channels-qq.mjs
import { chromium } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";

const PORT = process.env.UI_PORT ?? "3000";
const OUT_DIR = resolve(process.cwd(), "..", "_design");
mkdirSync(OUT_DIR, { recursive: true });

const targets = [
  { theme: "dark", file: "phase5e-channels-qq-dark.png" },
  { theme: "light", file: "phase5e-channels-qq-light.png" },
];

const browser = await chromium.launch();
try {
  for (const t of targets) {
    const ctx = await browser.newContext({
      viewport: { width: 1440, height: 900 },
    });
    const page = await ctx.newPage();
    await page.goto(`http://localhost:${PORT}/channels/qq?theme=${t.theme}`, {
      waitUntil: "networkidle",
      timeout: 30_000,
    });
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
