"use strict";
// #430: group tiers in Containers, Edit and Priority tiers.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function load() {
  const c = { window: {}, console, URLSearchParams, Map, Set, document: { addEventListener() {} },
    $: () => null, esc: s => String(s ?? "").replaceAll("<", "&lt;"), STATE: { data: {} }, tip: t => t };
  vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  for (const f of ["web/js/ui.js", "web/js/views-workloads.js", "web/js/views-lifecycle.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  return c;
}

test("renaming a group sends its old name, so its tier moves with it", async () => {
  const c = load(), sent = [];
  c.api = async (path, init) => { sent.push([path, JSON.parse(init.body)]); return { detail: "1 workload moved to House", group: "House" }; };
  c.toast = () => {}; c.manageWorkloadGroups = () => {}; c.renderWorkloads = () => {}; c.closeModal = () => {};
  c.saveWorkloadGroup = c.window.saveWorkloadGroup;
  c.$ = sel => sel === "#wg_rename" ? { value: "House" } : null;
  c.window.__wgGroup = { items: [{ ns: "lab", name: "frigate" }], from: "Home" };
  c.window.wgRenameApply();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(sent, [["/api/workloads/group", { items: [{ ns: "lab", name: "frigate" }], group: "House", renamed_from: "Home" }]]);
});

const plan = (apps, groups = []) => ({ apps, groups, changes: 0, max: 50, skipped: [], warnings: [] });
const row = (id, extra) => ({ id, kind: "app", ns: "lab", name: id.split("/")[1], current: "normal", tier: "normal", suggested: "", why: "",
  managed: "", group: "", follows: false, wanted: "normal", source: "own", status: "in_line", ...extra });

test("a row that follows its group says where its tier comes from, and that it needs a restart", () => {
  const html = load().priorityHtml(plan([row("lab/ha", { group: "Home", follows: true, wanted: "critical", source: "group", status: "restart", tier: "critical" })]));
  assert.match(html, /critical, from Home · runs normal/);
  assert.match(html, /needs a restart/);
});

test("a tier of its own, and the default", () => {
  const html = load().priorityHtml(plan([row("lab/db", { current: "low", tier: "low", wanted: "low" }), row("lab/web", { follows: true, source: "default" })]));
  assert.match(html, /low, its own · in line/);
  assert.match(html, /normal, the default · in line/);
});

test("an app Flux applies shows the line to commit", () => {
  const html = load().priorityHtml(plan([row("lab/web", { managed: "Flux", group: "Home", wanted: "critical", source: "group", status: "git",
    line: "spec.template.spec.priorityClassName: x-critical" })]));
  assert.match(html, /change in git/);
  assert.match(html, /spec\.template\.spec\.priorityClassName: x-critical/);
});

test("a class changed outside Homestead, and a group tier that can't be read, are said and not ticked", () => {
  const html = load().priorityHtml(plan([
    row("lab/ha", { group: "Home", follows: true, current: "low", tier: "low", wanted: "critical", source: "group", status: "outside" }),
    row("lab/fr", { group: "Home", follows: true, wanted: null, source: "group", status: "unknown_group" })]));
  assert.match(html, /changed outside Homestead/);
  assert.match(html, /group tier unknown/);
  assert.doesNotMatch(html, /was low|was normal/);
});

test("the groups are listed with their tiers", () => {
  const html = load().priorityHtml(plan([], [{ name: "Home", tier: "critical", apps: 4 }, { name: "Media", tier: "", apps: 1 }, { name: "Lab", tier: null, apps: 2 }]));
  assert.match(html, /<b>Home<\/b> critical · 4 apps/);
  assert.match(html, /<b>Media<\/b> no tier · 1 app</);
  assert.match(html, /<b>Lab<\/b> tier unknown · 2 apps/);
});

test("a row whose group tier is unknown has its select disabled, says why, and is not ticked", () => {
  const html = load().priorityHtml(plan([row("lab/fr", { group: "Home", follows: true, wanted: null, source: "group", status: "unknown_group" })]));
  assert.match(html, /<select[^>]*disabled/);
  assert.match(html, /Group tiers couldn&#39;t be read|Group tiers couldn't be read/);
  assert.doesNotMatch(html, /was normal/);
});

const field = (c, w) => c.placementSection({ apart: [], together: [] }, { failover: "default", ...w }, [], []);

test("Edit offers its group's tier first, for an app in a group", () => {
  const html = field(load(), { priority: "group", priority_group: "Home", priority_group_tier: "critical" });
  assert.match(html, /<option value="group" selected>From its group: critical, from Home<\/option>/);
  assert.match(html, /takes Home's tier/);
});

test("a group with no tier is offered as normal", () => {
  const html = field(load(), { priority: "low", priority_group: "Home", priority_group_tier: "" });
  assert.match(html, /value="group"\s*>From its group: normal, Home has no tier/);
  assert.match(html, /<option value="low" selected>/);
});

test("an app in no group has no group option", () => {
  assert.doesNotMatch(field(load(), { priority: "normal", priority_group: "" }), /value="group"/);
});

test("an app Flux applies stays read-only, in a group too", () => {
  const html = field(load(), { priority: "normal", priority_class: "x-normal", priority_owner: "Flux", priority_group: "Home", priority_group_tier: "critical" });
  assert.doesNotMatch(html, /id="e_priority"/);
});
