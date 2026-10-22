"use strict";

/* Platform versions: the header pill has to agree with the rows under it. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function card(report) {
  const ctx = { console, Date, Map, Set, Promise, document: { addEventListener() {} }, STATE: { data: {} },
    $: () => null, $$: () => [], icon: () => "", safeHref: String,
    esc: v => String(v ?? "").replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`) };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/views-cluster.js", "utf8"), ctx);
  const html = vm.runInContext("componentsCard", ctx)(report);
  return { html, pill: (html.match(/<span class="ui-chip [^"]*">([^<]*)<\/span>/) || [])[1] };
}
const part = (id, fields) => ({ id, name: id, installed: "v1.0.0", newest: "v1.0.0", next: "", behind: false, ...fields });

test("parts that Harvester upgrades and that are behind don't read as up to date", () => {
  const { pill } = card({ harvester: true, components: [
    part("longhorn", { newest: "v1.9.1", behind: true, how: "harvester" }),
    part("kubevirt", { newest: "v1.6.0", behind: true, how: "harvester" }),
    part("cdi", { newest: "v1.62.0", behind: true, how: "harvester" })] });
  assert.notEqual(pill, "up to date");
  assert.match(pill, /3 newer.*Harvester/);
});

test("a part installed outside Homestead that is behind says who upgrades it", () => {
  const { pill } = card({ components: [part("multus", { newest: "v4.2.0", behind: true, how: "manual" })] });
  assert.match(pill, /1 newer.*outside Homestead/);
});

test("an upgrade Homestead can start still leads", () => {
  const { pill } = card({ components: [part("longhorn", { newest: "v1.9.1", next: "v1.9.1", behind: true, how: "helmchart" }),
    part("multus", { newest: "v4.2.0", behind: true, how: "manual" })] });
  assert.equal(pill, "1 update");
});

test("nothing behind reads as up to date", () => {
  assert.equal(card({ components: [part("longhorn", { how: "helmchart" })] }).pill, "up to date");
});

test("every release-notes link has the same label", () => {
  const { html } = card({ components: [part("longhorn", { how: "helmchart", notes_url: "https://example.com/a" }),
    part("kubevirt", { how: "helmchart", notes_url: "https://example.com/b" })] });
  assert.doesNotMatch(html, />Notes</);
  assert.equal((html.match(/Release notes/g) || []).length, 2);
});
