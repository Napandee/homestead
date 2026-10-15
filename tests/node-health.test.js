"use strict";

/* A host's state is worked out once and said the same everywhere, and a host
   that isn't answering shows no current-looking readings. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const core = fs.readFileSync("web/js/core.js", "utf8");

function context() {
  const c = { document: { addEventListener() {}, querySelector() { return null; } }, FLEET: { view: {} }, icon: () => "",
    clusterAttr: () => "", clusterTag: () => "", fleetAll: () => false, Diagram: null,
    meter: () => "", sev: () => "", hardwareTags: () => "", remoteRow: () => "", sizePair: (used, cap) => `${used}/${cap} GB`, ratePair: (rx, tx) => [`↓${rx}`, `↑${tx}`] };
  c.window = c; vm.createContext(c);
  vm.runInContext(core.slice(0, core.indexOf("/* Put text on the clipboard.")), c);
  require("./helpers/core-text")(c);
  for (const file of ["metrics-utils.js", "ui.js", "views-overview.js"]) vm.runInContext(fs.readFileSync(`web/js/${file}`, "utf8"), c);
  return c;
}
const base = { name: "host-2", status: "Ready", schedulable: true, roles: ["worker"], cpu_pct: 12, cpu_cap: 4, mem_pct: 40,
  mem_used_gb: 6, mem_cap_gb: 16, fs_pct: 30, fs_used_gb: 30, fs_cap_gb: 100, rx_mbps: 1, tx_mbps: 1, pods: 9, disks: [],
  temps: { cpu_c: 41, max_c: 44, disks: [] }, workloads: [] };
const failing = { ...base, temps: { ...base.temps, disks: [{ name: "sda", health: { state: "attention", summary: "24 reallocated sectors" } }] } };
const down = { ...base, name: "host-3", status: "NotReady", cpu_pct: 0, mem_pct: 0, mem_used_gb: 0, fs_pct: undefined,
  fs_used_gb: undefined, fs_cap_gb: undefined, rx_mbps: undefined, tx_mbps: undefined, temps: { disks: [] } };

test("nodeHealth works out a host's state once", () => {
  const c = context();
  assert.deepEqual(JSON.parse(JSON.stringify(c.nodeHealth(base))), { tone: "ok", status: "Ready", issues: [], ready: true });
  const drive = c.nodeHealth(failing);
  assert.equal(drive.status, "Drive warning"); assert.equal(drive.tone, "warn");
  assert.deepEqual([...drive.issues], ["sda: 24 reallocated sectors"]);
  assert.equal(c.nodeHealth(down).status, "Not ready"); assert.equal(c.nodeHealth(down).tone, "bad");
  assert.equal(c.nodeHealth({ ...base, schedulable: false }).status, "Cordoned");
});

test("Nodes and the node's page say Drive warning for a Ready host with a failing drive", () => {
  const c = context();
  const status = vm.runInContext("nodeComparisonRows", c)([failing]).find(row => row.label === "Status").values[0];
  assert.match(status, /Drive warning/);
  assert.match(c.nodePageSummary(failing), /Drive warning/);
});

test("a host that isn't Ready shows no readings, only dashes", () => {
  const c = context();
  const summary = c.nodePageSummary(down);
  assert.doesNotMatch(summary, /undefined|\b0%/);
  assert.match(summary, /CPU <b>—<\/b>/);
  const rows = vm.runInContext("nodeComparisonRows", c)([down]);
  for (const label of ["CPU", "Memory", "Network", "Temp"]) assert.doesNotMatch(rows.find(row => row.label === label).values[0], /\b0%|undefined|°C/, label);
  const card = c.dashboardNodes([down], "compact");
  assert.doesNotMatch(card, /<b class="mono">0%<\/b>/);
});

test("the node's Storage line counts drive warnings, and Overview has one callout to Health", () => {
  const c = context();
  assert.equal(c.nodeStorageLine(failing, 2, 0), "2 drives · 1 warning");
  assert.equal(c.nodeStorageLine(base, 2, 1), "2 drives · 1 unused");
  const callout = c.nodeAttention(failing);
  assert.equal((callout.match(/class="ui-callout /g) || []).length, 1);
  assert.match(callout, /sda: 24 reallocated sectors/);
  assert.match(callout, /HealthInsights\.open\(&#39;health&#39;\)|HealthInsights\.open\('health'\)/);
  assert.match(c.nodeAttention(down), /ui-callout bad[\s\S]*not answering/);
  assert.equal(c.nodeAttention(base), "");
});
