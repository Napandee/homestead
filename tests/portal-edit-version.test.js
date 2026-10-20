"use strict";
// Edit links saves the whole list, so it names the version of the links it
// opened on: links changed elsewhere meanwhile are refused, not saved over (#621).
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function load(answers) {
  const sent = [], el = () => ({ innerHTML: "", textContent: "", disabled: false, focus() {} });
  const nodes = {};
  const c = { window: {}, console, Map, Set, Promise, JSON, sent, STATE: { data: {}, view: "dashboard" },
    document: { addEventListener() {} }, esc: s => String(s ?? ""), icon: () => "", tip: t => t,
    $: sel => (nodes[sel] = nodes[sel] || el()), $$: () => [], modal() {}, closeModal() {}, toast(...args) { c.toasts.push(args); },
    toasts: [], UI: { actions: s => s, error: e => String(e.message), pageHeader: () => "" }, loadAppIcons() {}, appAvatar: () => "",
    api: async (path, opts = {}) => {
      sent.push({ path, method: opts.method || "GET", body: opts.body ? JSON.parse(opts.body) : null });
      const answer = answers(path, opts.method || "GET", sent);
      if (answer instanceof Error) throw answer;
      return answer;
    } };
  vm.createContext(c);
  vm.runInContext(fs.readFileSync("web/js/views-portal.js", "utf8"), c);
  return c;
}

const links = [{ id: "0000000a", title: "Gateway", url: "http://192.0.2.1", section: "", icon: "", note: "" }];

test("a save names the version of the links the editor opened on", async () => {
  const c = load((path, method) => path === "/api/portal" && method === "GET" ? { links, version: "41" }
    : path === "/api/portal" ? { ok: true, links, version: "42" } : []);
  await c.window.portalEdit();
  await c.window.portalSave();
  const save = c.sent.find(r => r.method === "POST");
  assert.equal(save.body.version, "41");
  assert.deepEqual(save.body.links.map(l => l.title), ["Gateway"]);
});

test("links changed elsewhere are said, and the editor stays open to try again", async () => {
  const c = load((path, method) => method === "GET" ? (path === "/api/portal" ? { links, version: "41" } : [])
    : Object.assign(new Error("The portal links were changed elsewhere since you opened them"), { status: 409 }));
  await c.window.portalEdit();
  await c.window.portalSave();
  assert.match(String(c.toasts.at(-1)[0]), /changed elsewhere/);
  assert.equal(c.$("#pe_save").disabled, false);
});
