"use strict";
// The "if a host fails" room: the Settings list, and the preview's own section.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function setup(reply) {
  const fields = {};
  const ctx = { console, Map, Set, Date, Promise, URLSearchParams, encodeURIComponent, document: { addEventListener() {} },
    STATE: { data: {} }, $: key => fields[key], $$: () => [], can: () => true, toast: () => {}, tip: t => t,
    esc: value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    actionBar: items => items.map(i => i.label).join(""), api: async () => reply() };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx); ctx.jsq = value => ctx.esc(JSON.stringify(value));
  for (const file of ["web/js/impact.js", "web/js/views-settings.js"]) vm.runInContext(fs.readFileSync(file, "utf8"), ctx);
  ctx.capacityVerdict = vm.runInContext("capacityVerdict", ctx);
  return { ctx, fields };
}

const forecast = { node: "node2", verdict: "waits", moving: 3, unreserved: 1, sentence: "If node2 fails, node1 would have 14 of 15 GiB of memory reserved; 1 app would wait for room or be evicted.",
  hosts: [{ name: "node1", mem_gib: 14, mem_before_gib: 8, mem_alloc_gib: 15, cpu_m: 4000, cpu_alloc_m: 8000, full: 0.93, received: true }],
  waits: [], evicted: [{ id: "lab/media", tier: "normal", why: "evicted from node1 to make room for lab/ha", on: "node1", mem_gib: 4 }] };

test("each verdict has its word and tone", () => {
  const { ctx } = setup(() => ({}));
  assert.deepEqual(["ok", "fits"], [...ctx.capacityVerdict("fits")]);
  assert.deepEqual(["bad", "apps wait"], [...ctx.capacityVerdict("waits")]);
  assert.deepEqual(["neutral", "unknown"], [...ctx.capacityVerdict("unknown")]);
  assert.deepEqual(["bad", "no host left"], [...ctx.capacityVerdict("no-host")]);
});

test("the room section shows each host and an app evicted to make room", () => {
  const html = setup(() => ({})).ctx.capacityRoomHtml(forecast);
  assert.match(html, /8 → 14 of 15 GiB reserved/);
  assert.match(html, /lab\/media[^]*evicted from node1 to make room for lab\/ha[^]*normal tier/);
  assert.match(html, /1 of the 3 apps that move reserves nothing/);
});

test("the Settings list says how many apps reserve nothing", async () => {
  const t = setup(() => ({ unreserved: 4, pending: 0, hosts: [forecast] }));
  t.fields["#capacityForecast"] = {};
  await t.ctx.capacityForecastLoad();
  assert.match(t.fields["#capacityForecast"].innerHTML, /4 apps reserve nothing and fit anywhere on paper/);
});

test("a preview opened for another host meanwhile keeps its own room", async () => {
  const t = setup(() => ({ hosts: [forecast] }));
  t.fields["#ifDownRoom"] = { dataset: { node: "node3" }, innerHTML: "" };
  await vm.runInContext("ifDownRoom", t.ctx)("node2");
  assert.equal(t.fields["#ifDownRoom"].innerHTML, "");
  t.fields["#ifDownRoom"].dataset.node = "node2";
  await vm.runInContext("ifDownRoom", t.ctx)("node2");
  assert.match(t.fields["#ifDownRoom"].innerHTML, /Room on the other hosts · apps wait/);
});
