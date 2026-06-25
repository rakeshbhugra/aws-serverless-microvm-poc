// Headless screenshot helper. Usage: node /opt/shot.cjs [url] [outPath]
// CommonJS + NODE_PATH so it resolves the globally-installed playwright.
// --no-sandbox because chromium runs as root inside the VM.
const { chromium } = require("playwright");

(async () => {
  const url = process.argv[2] || "http://localhost:3000";
  const out = process.argv[3] || "/workspace/screenshots/button.png";
  const browser = await chromium.launch({ args: ["--no-sandbox"] });
  const page = await browser.newPage();
  await page.goto(url, { waitUntil: "load", timeout: 20000 });
  await page.waitForTimeout(800);
  await page.screenshot({ path: out });
  await browser.close();
  console.log("saved " + out);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
