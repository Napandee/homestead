"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
function setup() {
  let html = ""; const calls = [], focused = [];
  const c = { console, Set, Map, Promise, document: { addEventListener() {} }, STATE: { data: {} },
    $: selector => ({ set innerHTML(v) { html = v; }, get innerHTML() { return html; }, focus() { focused.push(selector); } }), $$: () => [],
    esc: v => String(v ?? "").replaceAll("<", "&lt;"), moveSeveral: (ids, to) => calls.push([ids, to]), toast() {} };
  c.window = c; vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  for (const f of ["web/js/ui.js", "web/js/views-overview.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  return { c, calls, focused, html: () => html };
}
const NODE = { name: "k2", workloads: ["ha", "plex"], workload_ids: ["lab/ha", "media/plex"] };

test("chips move one at a time until Select is on", () => {
  const t = setup();
  const card = t.c.nodeWorkloadChips(NODE);
  assert.match(card, /moveWorkload\("ha","lab"\)/);
  assert.doesNotMatch(card, /type="checkbox"/);
});

test("in select mode the chips stay buttons, pressed when selected, and Move selected opens the review with them", () => {
  const t = setup();
  t.c.nodeSelect("k2", true);
  t.c.nodePick("k2", "media/plex", true);
  const card = t.c.nodeWorkloadChips(NODE);
  assert.doesNotMatch(card, /type="checkbox"/);
  assert.match(card, /<button type="button" class="tag movable"[^>]*aria-pressed="true"[^>]*data-id="media\/plex"/);
  assert.match(card, /<button type="button" class="tag movable"[^>]*aria-pressed="false"[^>]*data-id="lab\/ha"/);
  assert.doesNotMatch(card, /moveWorkload/, "a click selects, it does not move");
  assert.doesNotMatch(card, /⇄/);
  assert.match(card, /1 selected/);
  t.c.nodeMoveSelected("k2");
  // The array comes from the page's own context: compare its contents.
  assert.equal(JSON.stringify(t.calls), JSON.stringify([[["media/plex"], ""]]));
});

test("a click toggles a chip, and focus stays on it after the card is redrawn", () => {
  const t = setup();
  t.c.nodeRemember(NODE);
  t.c.nodeSelect("k2", true);
  t.c.nodeToggle("k2", "lab/ha");
  assert.match(t.html(), /aria-pressed="true"[^>]*data-id="lab\/ha"/);
  assert.match(t.html(), /1 selected/);
  assert.match(t.focused.at(-1), /data-id="lab\/ha"/);
  t.c.nodeToggle("k2", "lab/ha");
  assert.match(t.html(), /aria-pressed="false"[^>]*data-id="lab\/ha"/);
  assert.match(t.html(), /0 selected/);
});

test("outside select mode no chip is pressed", () => {
  const t = setup();
  assert.doesNotMatch(t.c.nodeWorkloadChips(NODE), /aria-pressed/);
});

test("Escape in the card ends select mode, but not while a dialog is open", () => {
  const t = setup();
  t.c.nodeRemember(NODE);
  const press = () => { const e = { key: "Escape", prevented: false, preventDefault() { this.prevented = true; }, stopPropagation() {} }; t.c.nodeChipsKey(e, "k2"); return e; };
  t.c.nodeSelect("k2", true);
  t.c.modalIsOpen = () => true;
  assert.equal(press().prevented, false);
  assert.match(t.html(), /selected/);
  t.c.modalIsOpen = () => false;
  assert.equal(press().prevented, true);
  assert.doesNotMatch(t.html(), /selected/);
  assert.match(t.html(), /moveWorkload/);
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
  assert.match(t.html(), /aria-pressed="false"/);
});
