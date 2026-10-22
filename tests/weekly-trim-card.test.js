"use strict";
// Data protection's Housekeeping card for Homestead's weekly trim (#591).
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function view() {
  const ctx = { URL, document: { addEventListener() {} }, STATE: { data: {} },
    esc: s => String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"),
    jsq: s => JSON.stringify(String(s)), fmtAt: v => `at ${v}`,
    UI: { chip: (label, tone) => `<span class="ui-chip ${tone}">${label}</span>`, guide: (t, b) => `<details>${t}${b}</details>`,
      section: (t, b) => `<section class="ui-section"><h4>${t}</h4>${b}</section>` } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/views-protect.js", "utf8"), ctx);
  return ctx;
}

async function demo(path) {
  const ctx = { URL, URLSearchParams, Response, Date, console, location: { search: "?demo=1", origin: "https://demo.example.com" },
    HOMESTEAD_DEMO: false, document: { body: null, addEventListener() {} },
    fetch: async () => { throw new Error("demo must never reach a real cluster"); } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/demo.js", "utf8"), ctx);
  return (await ctx.fetch(path)).json();
}

test("the card says what the last weekly trim trimmed and skipped, from the demo's answer", async () => {
  const d = await demo("/api/weekly-trim");
  const html = view().weeklyTrimCard(d);
  assert.match(html, /id="housekeeping"/);
  assert.match(html, /Trimmed 2 volumes, skipped 1 \(1 detached\)/);
  assert.match(html, /ui-chip ok/);
  assert.match(html, /Run now/);
  assert.match(html, /data-need="admin"[^>]*>[^<]*<input type="checkbox" id="wt_on" checked/);
  assert.match(html, /Skipped:.*detached/);
});

test("a failed volume is named with Longhorn's message, and escaped", () => {
  const html = view().weeklyTrimCard({ enabled: true, next: "2026-10-31T04:00:00Z", running: false, theirs: "",
    last: { at: 1, trimmed: [], skipped: [], unconfirmed: [], failed: [{ volume: "pvc-demo-c", pvc: "data-<c>", message: "engine is not running" }] } });
  assert.match(html, /ui-chip bad/);
  assert.match(html, /Failed: <b>data-&lt;c&gt;<\/b>: engine is not running/);
  assert.ok(!html.includes("data-<c>"));
});

test("off, never run, running, and a trim job of the user's each say so", () => {
  const ctx = view();
  assert.match(ctx.weeklyTrimCard({ enabled: false, next: "", last: null, running: false, theirs: "" }), /Off · Run now still trims/);
  assert.match(ctx.weeklyTrimCard({ enabled: true, next: "x", last: null, running: false, theirs: "" }), /Not run yet/);
  const running = ctx.weeklyTrimCard({ enabled: true, next: "x", last: null, running: true, theirs: "" });
  assert.match(running, /trimming now/);
  assert.doesNotMatch(running, /weeklyTrimRun\(\)/, "no second Run now while one runs");
  assert.match(ctx.weeklyTrimCard({ enabled: true, next: "x", last: null, running: false, theirs: "my-trim" }),
    /my-trim.*already trims the default group/);
});
