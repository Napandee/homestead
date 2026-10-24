"use strict";
// Data Protection's Restore tests keep the last list while they are read again,
// and the new list is morphed in, so unchanged rows stay the same elements (#834).
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");
const src = fs.readFileSync("web/js/views-protect.js", "utf8");

const LIST = { enabled: true, every_days: 30, apps: [{ ns: "lab", name: "paperless", icon: "", running: false, without: [],
  restores: [{ claim: "paperless-data", backup: "backup-1" }], last: { at: 1, ok: true, seconds: 60 } }] };

function view(answer = LIST) {
  const calls = { morph: [], enhanced: 0, roles: 0 };
  const host = { innerHTML: "OLD", cloneNode: deep => (assert.equal(deep, false), { innerHTML: "" }) };
  const ctx = { document: { addEventListener() {}, getElementById: id => id === "restore-tests" ? host : null },
    STATE: { data: {} }, setTimeout: () => 0, clearTimeout() {}, toast() {},
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    $: () => null, tip: t => t, icon: () => "", jsArg: s => JSON.stringify(String(s)), fmtAt: String,
    appAvatar: name => `<i>${name}</i>`, actionBar: () => "<span>actions</span>",
    enhanceActions: () => { calls.enhanced++; }, morph: (a, b) => calls.morph.push([a, b]),
    api: async path => path === "/api/restore-tests" ? JSON.parse(JSON.stringify(answer)) : { detail: "ok" } };
  ctx.jsq = s => ctx.esc(ctx.jsArg(s)); ctx.window = ctx; ctx.applyRole = () => { calls.roles++; };
  vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(src, ctx);
  return { ctx, host, calls };
}

test("Restore tests show the placeholder only before the first list", async () => {
  const { ctx } = view();
  assert.match(ctx.restoreTestsSection(), /Finding each app's newest backups/);
  ctx.STATE.data.restoreTests = LIST;
  const html = ctx.restoreTestsSection();
  assert.match(html, /paperless/);
  assert.doesNotMatch(html, /Finding each app's newest backups/);
});

test("the Data Protection page paints the Restore tests section", () => {
  const page = between(src, "async function viewProtect", "loadRestoreTests();");
  assert.match(page, /<div id="restore-tests" class="restore-tests">\$\{restoreTestsSection\(\)\}<\/div>/);
});

test("a new list is morphed in, prepared as the page paint prepares it", async () => {
  const { ctx, host, calls } = view();
  await ctx.loadRestoreTests();
  assert.equal(host.innerHTML, "OLD", "not rewritten: the rows stay the same elements");
  assert.equal(calls.morph.length, 1);
  assert.equal(calls.morph[0][0], host);
  assert.equal(calls.morph[0][1].innerHTML, ctx.restoreTestsSection(), "the same markup the page paints");
  assert.equal(calls.enhanced, 1, "enhanced as paint() enhances the page, so the two agree");
  assert.equal(calls.roles, 1, "roles applied again, so the admin switch stays hidden from a viewer");
});

test("switching restore tests on or off updates the kept list", async () => {
  const { ctx } = view();
  await ctx.loadRestoreTests();
  await ctx.restoreTestsSwitch(false);
  assert.equal(ctx.STATE.data.restoreTests.enabled, false, "the next paint does not tick the box again");
});
