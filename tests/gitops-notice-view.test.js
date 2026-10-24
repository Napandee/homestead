"use strict";
// #734: Edit, Move and Rename say when Flux or Argo CD applies the app, and a
// rename of such an app waits for its own tick.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

const FLUX = { by: "Flux", source: "Kustomization flux-system/apps", detected: true };
const capacity = { blocked: false, requires_confirmation: true, candidates: [], warnings: [], additional: 1 };

function setup(review, preview = "/api/edit/preview") {
  let html = "";
  const sent = [], notices = [], fields = { "#editCapacityConfirm": { checked: true }, "#editManagedConfirm": { checked: false },
    "#editGo": {}, "#hostMoveConfirm": { checked: true }, "#hostMoveGo": {}, "#mbody": { insertAdjacentHTML() {} } };
  const c = { window: {}, console, URLSearchParams, Map, document: { addEventListener() {}, getElementById: id => fields["#" + id] || null },
    $: s => fields[s], esc: s => String(s).replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
    childModal: (_, body) => { html = body; }, modal: (_, body) => { html = body; }, toast: m => notices.push(m), closeModal() {}, refresh() {}, setTimeout() {},
    deployCapacityHtml: () => "capacity report",
    api: async (path, options) => {
      if (path === preview) return review;
      sent.push({ path, body: JSON.parse(options.body) });
      return { ok: true, name: "web2" };
    } };
  vm.createContext(c); require("./helpers/core-text")(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  for (const file of ["ui.js", "gitops.js", "views-workloads.js", "move.js", "views-lifecycle.js"])
    vm.runInContext(fs.readFileSync(`web/js/${file}`, "utf8"), c);
  return { c, fields, sent, notices, html: () => html };
}

test("The edit review says Flux applies the app and names its Kustomization", async () => {
  const t = setup({ capacity_token: "t", capacity, managed: FLUX });
  await t.c.window.editReview({ ns: "lab", name: "web" });
  assert.match(t.html(), /Flux applies this app \(Kustomization flux-system\/apps\)/);
  assert.match(t.html(), /lasts until Flux next applies it\. To keep it, change it in git\./);
  assert.doesNotMatch(t.html(), /editManagedConfirm/);
});

test("An app nothing else applies gets no note", async () => {
  const t = setup({ capacity_token: "t", capacity, managed: {} });
  await t.c.window.editReview({ ns: "lab", name: "web" });
  assert.doesNotMatch(t.html(), /applies this app|Marked as updated/);
});

test("An app marked as updated elsewhere names the mark, not Flux", async () => {
  const t = setup({ capacity_token: "t", capacity, managed: { by: "Renovate", source: "", detected: false } });
  await t.c.window.editReview({ ns: "lab", name: "web" });
  assert.match(t.html(), /Marked as updated by Renovate/);
  assert.doesNotMatch(t.html(), /Flux/);
});

test("Rename of a Flux app waits for its own tick, then says so to the server", async () => {
  const t = setup({ capacity_token: "t", capacity: { ...capacity, rename: { from: "web", to: "web2" } }, managed: FLUX });
  await t.c.window.editReview({ ns: "lab", name: "web", workload_name: "web2" });
  assert.match(t.html(), /id="editManagedConfirm"/);
  assert.match(t.html(), /Flux will bring back the old name/);
  await t.c.window.confirmEdit();
  assert.equal(t.sent.length, 0);
  assert.match(t.notices.at(-1), /old name/);
  t.fields["#editManagedConfirm"].checked = true;
  await t.c.window.confirmEdit();
  assert.equal(t.sent.length, 1);
  assert.equal(t.sent[0].body.confirm_managed, true);
});

test("Rename of a plain app sends no confirm_managed", async () => {
  const t = setup({ capacity_token: "t", capacity: { ...capacity, rename: { from: "web", to: "web2" } }, managed: {} });
  await t.c.window.editReview({ ns: "lab", name: "web", workload_name: "web2" });
  await t.c.window.confirmEdit();
  assert.equal(t.sent.length, 1);
  assert.equal("confirm_managed" in t.sent[0].body, false);
});

test("The move review carries the same note", async () => {
  const t = setup({ capacity_token: "t", managed: FLUX,
    capacity: { blocked: false, warnings: [], move: { node: "b", mode: "preferred", target: null } } }, "/api/move/preview");
  await t.c.window.hostMoveReview({ ns: "lab", name: "web", node: "b", pin: false });
  assert.match(t.html(), /Flux applies this app/);
});
