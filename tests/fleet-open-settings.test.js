"use strict";

/* "Manage clusters" opens Settings' Linked clusters section and reads the
   clusters once: on Settings already, choosing the section must not also
   load it, or /api/fleet and /api/fleet/legacy are read twice. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function open(view) {
  const calls = [];
  const ctx = { console, STATE: { view, data: {} }, document: { addEventListener() {}, removeEventListener() {} },
    $: () => null, $$: () => [], toast() {}, esc: String, location: { href: "" }, closeModal() {},
    settingsTab: (...args) => calls.push(["settingsTab", ...args]), go: page => calls.push(["go", page]) };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/fleet.js", "utf8"), ctx);
  ctx.fleetSettingsPaint = () => calls.push(["paint"]);
  ctx.fleetOpenSettings();
  calls.loaded = ctx.STATE.settingsLoaded;
  return calls;
}

test("on Settings, the section is shown without loading it and painted once", () => {
  const calls = open("settings");
  assert.deepEqual([...calls], [["settingsTab", "fleet", false], ["paint"]]);
  // Painted here, it counts as loaded: coming back to it does not read again.
  assert.ok(calls.loaded.has("fleet"));
});

test("from another page, Settings opens on the section and its own load paints it", () => {
  assert.deepEqual([...open("workloads")], [["settingsTab", "fleet"], ["go", "settings"]]);
});
