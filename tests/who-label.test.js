"use strict";
// UI.who: the name Homestead records for who did something, as a person reads it.
// A person relayed by a linked Homestead is "fleet:<handle>:<name>", that Homestead's
// own call "fleet:<handle>", an API key "api-key:<name>" and an agent request run on
// approval "approving:<key>:<name>": forms no account can have (a username cannot
// contain ':'), shown here in words.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function load(members) {
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const painted = [];
  const c = { console, esc, jsq: s => esc(JSON.stringify(String(s))), jsArg: s => JSON.stringify(String(s ?? "")), icon: name => `<i data-icon="${name}"></i>`,
    document: { getElementById: () => null }, STATE: { data: {} }, location: { search: "" }, URLSearchParams, JSON, Date, Math,
    setTimeout: () => 0, clearTimeout() {}, HomesteadRouter: { urlFor: v => "/" + v }, agoText: at => "ago " + at,
    paint: html => painted.push(html), toast() {}, modal() {}, closeModal() {}, $: () => ({}),
    api: async () => ({ requests: [{ id: "d1", tool: "delete_container", title: "Delete a container", status: "done", reason: "Clean up",
      created: 1000, expires: 4600, ended: 2000, decided_by: "fleet:office:alice", detail: "Deleted", role: "operator", escalated: false,
      key: "Claude Code", owner: "sam", addr: "192.0.2.31", action: "Delete the container lab/old-wiki", summary: {}, args: {}, can_decide: false }],
      pending: 0 }) };
  if (members) c.FLEET = { view: { members } };
  c.window = c; c.globalThis = c;
  vm.createContext(c);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), c);
  vm.runInContext(fs.readFileSync("web/js/views-agents.js", "utf8"), c);
  return { c, painted };
}

const OFFICE = [{ id: "a1", name: "Office", handle: "office", self: false }];

test("a person relayed by a linked Homestead is named with that Homestead", () => {
  const { c } = load(OFFICE);
  assert.equal(c.UI.who("fleet:office:alice"), "alice (via Office)");
  assert.equal(c.UI.who("fleet:office:alice@example.com"), "alice@example.com (via Office)");
});

test("a linked Homestead's own call is named as that Homestead", () => {
  assert.equal(load(OFFICE).c.UI.who("fleet:office"), "Office");
});

test("without the list of linked Homesteads, the handle stands in for the name", () => {
  assert.equal(load().c.UI.who("fleet:office:alice"), "alice (via office)");
});

test("an API key and an approved agent request read in words", () => {
  const { c } = load(OFFICE);
  assert.equal(c.UI.who("api-key:backup"), "API key backup");
  assert.equal(c.UI.who("approving:Claude Code:ada"), "ada approving Claude Code");
  assert.equal(c.UI.who("approving:Claude Code:fleet:office:alice"), "alice (via Office) approving Claude Code");
  assert.equal(c.UI.who("approving:Claude\u2236 laptop:ada"), "ada approving Claude\u2236 laptop", "a ':' in a key's name is recorded as '\u2236'");
});

test("a local account's name is shown as it is, @ and all", () => {
  const { c } = load(OFFICE);
  assert.equal(c.UI.who("ada"), "ada");
  assert.equal(c.UI.who("alice@office"), "alice@office");
  assert.equal(c.UI.who(""), "");
});

test("the agent requests page says who decided in words", async () => {
  const { c, painted } = load(OFFICE);
  await c.viewAgents();
  assert.ok(painted[0].includes("alice (via Office)"), "decided by the relayed person, in words");
  assert.ok(!painted[0].includes("fleet:office:alice"));
});

test("someone viewing this Homestead from a linked one sees their own name in words", () => {
  const fields = {};
  const c = { console, esc: s => String(s), document: { activeElement: null, body: { dataset: {} }, getElementById: () => null },
    $: s => fields[s] || (fields[s] = { textContent: "", className: "", addEventListener() {}, classList: { add() {}, remove() {}, contains: () => false } }),
    $$: () => [], localStorage: { getItem: () => null, setItem() {} }, URLSearchParams, HOMESTEAD_BASE: "/",
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {}, location: { pathname: "/", search: "" },
    FLEET: { view: { members: OFFICE } }, fetch: async () => ({ ok: true, json: async () => ({}) }) };
  c.window = c;
  vm.createContext(c);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), c);
  vm.runInContext(fs.readFileSync("web/js/router.js", "utf8"), c);
  vm.runInContext(fs.readFileSync("web/js/auth.js", "utf8").replace(/^boot\(\);$/m, ""), c);
  vm.runInContext('ME = "fleet:office:alice"; ROLE = "operator"; paintWho();', c);
  assert.equal(fields["#whonm"].textContent, "alice (via Office)");
  assert.equal(fields["#whoav"].textContent, "AL");
});
