// Demo-only browser check: the top bar's search finds VMs and pages, is used
// with the keyboard, and opens the item picked.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { chromium } from "playwright";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";
const output = process.env.SEARCH_SCREENSHOTS || "release-assets/pages/global-search";

await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(`${base}/?demo=1`, { waitUntil: "networkidle" });
  const search = page.locator("#globalSearch"), results = page.locator("#searchResults");
  const vm = await page.evaluate(async () => (await api("/api/vms"))[0]);

  // A VM by name, opened with the keyboard.
  await search.fill(vm.name);
  await page.locator("#searchResults .sresult").first().waitFor();
  assert.equal(await search.getAttribute("aria-expanded"), "true");
  assert.match(await page.locator("#searchResult0").innerText(), new RegExp(`${vm.name}[\\s\\S]*VM`));
  await search.press("ArrowDown");
  assert.equal(await search.getAttribute("aria-activedescendant"), "searchResult0");
  assert.equal(await page.locator("#searchResult0").getAttribute("aria-selected"), "true");
  await page.screenshot({ path: `${output}/vm.png` });
  await search.press("Enter");
  await page.waitForFunction(() => STATE.view === "vms");
  await page.locator("#modal:not(.hidden) #mtitle").filter({ hasText: vm.name }).waitFor();
  await page.evaluate(() => closeModal());

  // A Settings section and a page's own section, and Escape closes the list.
  await search.fill("updates");
  await page.locator("#searchResults .sresult").first().waitFor();
  const kinds = await page.locator("#searchResults .sresult small").allInnerTexts();
  assert.ok(kinds.some(text => text.startsWith("Settings")), kinds.join(" | "));
  await search.fill("firewall");
  await page.locator("#searchResults .sresult small").filter({ hasText: "Networking › Firewall" }).waitFor();
  await search.press("Escape");
  assert.equal(await results.isHidden(), true);
  assert.equal(await search.getAttribute("aria-expanded"), "false");
  await search.fill("networking");
  await page.locator("#searchResults .sresult").first().waitFor();
  await search.press("ArrowUp");
  await search.press("ArrowDown");
  await search.press("Enter");
  await page.waitForFunction(() => STATE.view === "network");
  assert.deepEqual(errors, []);
  console.log("Global search: VM found and opened by keyboard, Settings and pages found, Escape closes the list");
} finally { await browser.close(); }
