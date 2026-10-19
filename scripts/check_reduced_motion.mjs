// Demo-only browser check: with Homestead's motion setting off, or the system
// asking for reduced motion, nothing slides or scales in - a dialog, a
// confirmation, the sign-in box, a toast or a page's sections. With motion on
// they still do. It reads each element's computed animation name, so it does
// not depend on catching an animation while it runs.
import assert from "node:assert/strict";
import { chromium } from "playwright";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";

// One element of each kind that moves in, and the animation it has with motion on.
const MOVING = { modalbox: "pop", askbox: "pop", gatebox: "pop", tst: "slide", "stagger-child": "fu" };

async function look(contextOptions, motion) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, ...contextOptions });
  await context.addInitScript(m => localStorage.setItem("homestead.settings",
    JSON.stringify({ theme: "dark", bg: "soft", blur: 26, motion: m, refresh: 60 })), motion);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(`${base}/?demo=1`, { waitUntil: "networkidle" });
  const names = await page.evaluate(kinds => {
    const host = document.createElement("div");
    host.innerHTML = '<div class="modalbox"></div><div class="askbox"></div><div class="gatebox"></div>'
      + '<div class="tst"></div><div class="stagger"><div class="stagger-child"></div></div>';
    document.body.appendChild(host);
    const out = {};
    for (const kind of kinds) out[kind] = getComputedStyle(host.querySelector("." + kind)).animationName;
    host.remove();
    return out;
  }, Object.keys(MOVING));
  assert.deepEqual(errors, [], "no page errors");
  await context.close();
  return names;
}

const browser = await chromium.launch({ headless: true });
try {
  const none = Object.fromEntries(Object.keys(MOVING).map(kind => [kind, "none"]));
  assert.deepEqual(await look({}, "off"), none, "motion off: nothing slides or scales in");
  console.log("Reduced motion passed: motion off");
  // The system asks for less motion while Homestead's own setting stays on.
  assert.deepEqual(await look({ reducedMotion: "reduce" }, "on"), none, "reduced motion: nothing slides or scales in");
  console.log("Reduced motion passed: reduced motion");
  // The control: with motion on and no preference, each still moves in.
  assert.deepEqual(await look({}, "on"), MOVING, "motion on: each still has its animation");
  console.log("Reduced motion passed: motion on keeps the animations");
} finally {
  await browser.close();
}
