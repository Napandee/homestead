"use strict";
// If this host goes down (#440): a StatefulSet's row - its chip, its copies
// here, the link to Longhorn's setting - and rows that couldn't be checked.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function setup(payload = {}) {
  const fields = { "#mbody": { innerHTML: "" } }, calls = [];
  const ctx = { console, Map, Set, Promise, encodeURIComponent, document: { addEventListener() {} }, STATE: { data: {} },
    $: key => fields[key], $$: () => [], can: () => true, tip: t => t, toast: () => {},
    esc: value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    appAvatar: () => '<span class="av"></span>', modal: () => {},
    closeModal: () => calls.push(["closeModal"]), settingsTab: tab => calls.push(["settingsTab", tab]), go: view => calls.push(["go", view]),
    api: async url => (String(url).startsWith("/api/nodes/impact") ? payload : { hosts: [] }) };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(fs.readFileSync("web/js/impact.js", "utf8"), ctx);
  return { ctx, fields, calls, impactApp: vm.runInContext("impactApp", ctx) };
}

const WAITS = "waits until its pod is deleted: Kubernetes doesn't start a StatefulSet's pod elsewhere while the old one might still run. "
  + "Longhorn can do this for you: Settings → Hardware and storage → Longhorn storage → Pods on a failed node.";
const waits = { kind: "set", ns: "data", name: "postgres", icon: "", here: 1, copies: 3, keeps_answering: true,
  outcome: "waits", setting: "node_down", why: WAITS, notes: [] };

test("a StatefulSet's row has its chip, its copies here and a link to Longhorn's setting", () => {
  const html = setup().impactApp(waits);
  assert.match(html, /<span class="tag">StatefulSet<\/span>/);
  assert.match(html, /data · 1 of 3 copies here · keeps answering from its other copies/);
  assert.match(html, /<button class="linkish xs" onclick="ifDownSetting\(\)">Open the setting<\/button>/);
  assert.match(html, /pill slim warn">waits</);
});

test("one copy reads copy, and a row the setting can't help has no link", () => {
  const html = setup().impactApp({ ...waits, copies: 1, keeps_answering: false, setting: undefined, outcome: "stops",
    why: "its volume data-postgres-0 is stored on node2" });
  assert.match(html, /data · 1 of 1 copy here</);
  assert.doesNotMatch(html, /ifDownSetting/);
});

test("names and reasons are escaped", () => {
  const html = setup().impactApp({ ...waits, name: "<img src=x onerror=alert(1)>", ns: 'x-db"', why: "<b>x</b>",
    notes: ["<i>n</i>"] });
  assert.doesNotMatch(html, /<img src=x|<b>x<\/b>|<i>n<\/i>/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html, /x-db&quot;/);
});

test("the link opens Settings where Longhorn's setting is", () => {
  const t = setup();
  t.ctx.ifDownSetting();
  assert.deepEqual(t.calls, [["closeModal"], ["settingsTab", "cluster"], ["go", "settings"]]);
});

test("rows that couldn't be checked have their own chip and section; unread StatefulSets are said", async () => {
  const t = setup({ node: "node2", others_ready: ["node1"], at_risk: [], fewer_copies: [], addresses: [], unread: ["StatefulSets"],
    counts: { stops: 0, waits: 0, unknown: 1, moves: 0, at_risk: 0, fewer_copies: 0, addresses: 0 },
    apps: [{ ...waits, outcome: "unknown", setting: undefined, why: "couldn't be checked: where its volumes are stored" }] });
  await t.ctx.nodeIfDown("node2");
  const html = t.fields["#mbody"].innerHTML;
  assert.match(html, /<span class="pill neutral">1 unknown<\/span>/);
  // Titles and reasons go through esc, which turns ' into &#39;.
  assert.match(html, /Couldn(?:'|&#39;)t be checked<\/h4>[^]*couldn&#39;t be checked: where its volumes are stored/);
  assert.match(html, /pill slim neutral">unknown</);
  assert.match(html, /Couldn't read StatefulSets, so none of them is listed\./);
});
