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
