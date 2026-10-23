"use strict";
// Starts after (#746) in the web UI: Edit → Placement's block, what Edit
// sends, the review line, and what the Apps list and Start say while an app
// waits for another.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function load(extra = {}) {
  const c = { window: {}, console, URLSearchParams, Map, Set, Date, document: { addEventListener() {} },
    $: () => null, $$: () => [], esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    STATE: { data: {} }, tip: t => `<span class="tip">${t}</span>`, icon: () => "",
    // core.js's fmtClock takes seconds or ms; local time, here UTC for a fixed answer.
    fmtClock: v => new Date(v < 1e12 ? v * 1000 : v).toISOString().slice(11, 16), ...extra };
  vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => c.esc(c.jsArg(s));
  for (const f of ["web/js/ui.js", "web/js/views-workloads.js", "web/js/views-lifecycle.js", "web/js/views-storage.js"])
    vm.runInContext(fs.readFileSync(f, "utf8"), c);
  return c;
}

const candidates = [
  { ns: "lab", app: "postgres", services: [{ name: "postgres", ports: [5432] }] },
  { ns: "lab", app: "frigate", services: [{ name: "frigate", ports: [5000, 8554] }] },
  { ns: "data", app: "redis", services: [{ name: "redis", ports: [6379] }] },
  { ns: "lab", app: "worker", services: [] },
];
const form = (over = {}) => ({ after: [], timeout: 300, on_timeout: "start", unreadable: false, managed_by: null,
  gate: "none", candidates, git: "", ...over });
const ctx = { ns: "lab", name: "app-1" };

test("the block lists the stored apps, with other namespaces and a port choice where there is one", () => {
  const c = load();
  const html = c.startsAfterBlock(form({ after: [{ ns: "lab", app: "frigate", service: "frigate", port: 8554 }], gate: "rendered" }), ctx);
  assert.match(html, /Starts after/);
  assert.match(html, /<option value="lab\/frigate" selected>frigate<\/option>/);
  assert.match(html, /redis · data/);
  assert.match(html, /<option value="frigate:8554" selected>8554<\/option>/);
  assert.match(html, /value="5"/, "300 s is shown as 5 minutes");
  assert.match(html, /Start anyway/);
  assert.match(html, /Keep waiting/);
  assert.match(html, /<option value="lab\/worker" disabled>worker · no Service with a TCP port<\/option>/);
});

test("an app that has gone stays in its row, marked", () => {
  const html = load().startsAfterBlock(form({ after: [{ ns: "lab", app: "old-db", service: "old-db", port: 5432 }] }), ctx);
  assert.match(html, /<option value="lab\/old-db" selected>old-db \(gone\)<\/option>/);
});

test("a name is escaped", () => {
  const html = load().startsAfterBlock(form({ after: [{ ns: "lab", app: "<b>x", service: "s", port: 1 }] }), ctx);
  assert.doesNotMatch(html, /<b>x/);
  assert.match(html, /&lt;b>x/);
});

test("an app Flux applies shows the lines to commit and offers to show them for a change", () => {
  const html = load().startsAfterBlock(form({ managed_by: "Flux", after: [{ ns: "lab", app: "postgres", service: "postgres", port: 5432 }],
    gate: "not-committed", git: "metadata:\n  annotations:\n    homestead.io/starts-after: x\n" }), ctx);
  assert.match(html, /<pre[^>]*id="e_starts_git"[^>]*>metadata:/);
  assert.match(html, /where Flux applies it/);
  assert.match(html, /Show the lines to commit/);
});

test("a read that failed says so and offers nothing to change", () => {
  const html = load().startsAfterBlock({ error: "the API timed out" }, ctx);
  assert.match(html, /Could not read Starts after: the API timed out/);
  assert.match(html, /Retry/);
  assert.doesNotMatch(html, /id="e_starts_rows"/);
});

test("a setting edited by hand is said, and can be cleared", () => {
  const html = load().startsAfterBlock(form({ unreadable: true }), ctx);
  assert.match(html, /can't be read/);
  assert.match(html, /Clear it/);
});

test("Homestead and its helpers are told they can't wait", () => {
  const html = load().startsAfterBlock(form(), { ...ctx, fixed: true });
  assert.match(html, /can't wait for other apps/);
  assert.doesNotMatch(html, /id="e_starts_rows"/);
});

test("what Edit sends: nothing untouched, minutes as seconds when changed, an empty list when cleared", () => {
  const c = load();
  const base = form({ after: [{ ns: "lab", app: "postgres", service: "postgres", port: 5432 }] });
  const rows = [{ ns: "lab", app: "postgres", service: "postgres", port: 5432 }];
  assert.equal(c.startsAfterValue(base, { dirty: false, rows, minutes: 5, on_timeout: "start" }), undefined);
  assert.equal(c.startsAfterValue(base, { dirty: true, rows, minutes: 5, on_timeout: "start" }), undefined, "the same again is no change");
  assert.deepEqual(JSON.parse(JSON.stringify(c.startsAfterValue(base, { dirty: true, rows, minutes: 2.5, on_timeout: "wait" }))),
    { after: rows, timeout: 150, on_timeout: "wait" });
  assert.deepEqual(JSON.parse(JSON.stringify(c.startsAfterValue(base, { dirty: true, rows: [], minutes: 5, on_timeout: "start" }))),
    { after: [], timeout: 300, on_timeout: "start" });
  assert.equal(c.startsAfterValue({ error: "x" }, { dirty: true, rows, minutes: 5, on_timeout: "start" }), undefined, "never after a failed read");
  assert.equal(c.startsAfterValue(form({ managed_by: "Flux" }), { dirty: true, rows, minutes: 5, on_timeout: "start" }), undefined, "never on a managed app");
  assert.ok(c.startsAfterValue(form({ unreadable: true }), { dirty: true, rows: [], minutes: 5, on_timeout: "start" }), "clearing a hand-edited one is a change");
});

test("the review says what saving does", () => {
  const c = load();
  assert.equal(c.startsAfterReviewLine("app-1", { after: [{ app: "postgres" }, { app: "redis" }] }),
    "Saving restarts app-1 once: its pods wait for postgres and redis before starting.");
  assert.equal(c.startsAfterReviewLine("app-1", { after: [] }), "Saving restarts app-1 once: its pods no longer wait for other apps.");
  assert.equal(c.startsAfterReviewLine("app-1", undefined), "");
});

test("a refusal about Starts after is recognised; others are not", () => {
  const c = load();
  for (const text of ["a cycle: app-1 → app-2 → app-1", "app-2 has no Service with a TCP port, so nothing can tell when it is ready",
    "app-2's Service has no port 9", "app-1 does not use cluster DNS (dnsPolicy Default), so it can't find app-2; set dnsPolicy to ClusterFirst",
    "an app can't start after itself", "wait up to must be between 30 s and 60 min"])
    assert.ok(c.startsAfterRefusal(text), text);
  assert.ok(!c.startsAfterRefusal("Every container needs a name and image"));
});

test("which picked apps are stopped, so a start would wait", () => {
  const c = load();
  const others = [{ ns: "lab", name: "postgres", desired: 0 }, { ns: "lab", name: "frigate", desired: 1 }];
  assert.deepEqual([...c.startsAfterStopped([{ ns: "lab", app: "postgres" }, { ns: "lab", app: "frigate" }, { ns: "lab", app: "gone" }], others)],
    ["postgres", "gone"]);
});
