"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
function setup() {
  let html = ""; const calls = [];
  const c = { console, Set, Map, Promise, document: { addEventListener() {} }, STATE: { data: {} },
    $: () => ({ set innerHTML(v) { html = v; }, get innerHTML() { return html; } }), $$: () => [],
    esc: v => String(v ?? "").replaceAll("<", "&lt;"), moveSeveral: (ids, to) => calls.push([ids, to]), toast() {} };
  c.window = c; vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  for (const f of ["web/js/ui.js", "web/js/views-overview.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  return { c, calls, html: () => html };
}
const NODE = { name: "k2", workloads: ["ha", "plex"], workload_ids: ["lab/ha", "media/plex"] };

test("chips move one at a time until Select is on", () => {
  const t = setup();
  const card = t.c.nodeWorkloadChips(NODE);
  assert.match(card, /moveWorkload\("ha","lab"\)/);
  assert.doesNotMatch(card, /type="checkbox"/);
});

test("in select mode chips are tick boxes, and Move selected opens the review with them", () => {
  const t = setup();
  t.c.nodeSelect("k2", true);
  t.c.nodePick("k2", "media/plex", true);
  const card = t.c.nodeWorkloadChips(NODE);
  assert.match(card, /type="checkbox"[^>]*value="media\/plex"[^>]*checked/);
  assert.match(card, /1 selected/);
  t.c.nodeMoveSelected("k2");
  // The array comes from the page's own context: compare its contents.
  assert.equal(JSON.stringify(t.calls), JSON.stringify([[["media/plex"], ""]]));
});

test("Select all ticks every app on the host", () => {
  const t = setup();
  t.c.nodeSelect("k2", true);
  t.c.nodePickAll("k2", true, NODE.workload_ids);
  assert.match(t.c.nodeWorkloadChips(NODE), /2 selected/);
});

test("select mode works on a host page loaded directly, with no host list", () => {
  const t = setup();
  t.c.nodeRemember(NODE);
  t.c.nodeSelect("k2", true);
  assert.match(t.html(), /type="checkbox"/);
});
