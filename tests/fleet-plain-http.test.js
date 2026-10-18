"use strict";

/* Linking sends the admin password there and then the clusters' shared key.
   Over plain http:// that has to be said and agreed to; clusters linked or
   added over plain http:// are marked (#602). */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function setup(fields = {}) {
  const calls = [], toasts = [], asked = [];
  const ctx = { console, Promise, JSON, STATE: { view: "settings", data: {} },
    document: { addEventListener() {} }, $: id => fields[id] || null, $$: () => [],
    esc: s => String(s ?? "").replaceAll("<", "&lt;").replaceAll('"', "&quot;"), icon: () => "",
    localStorage: { getItem: () => null, setItem() {} }, location: { pathname: "/", search: "" },
    toast: (m, k) => toasts.push({ m, k }), closeModal() {}, fleetRefreshAfter: async () => {},
    ask: async message => { asked.push(message); return false; },
    api: async (path, opts) => { calls.push({ path, body: opts?.body ? JSON.parse(opts.body) : null }); return { member: { name: "branch" } }; } };
  ctx.window = ctx; vm.createContext(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/fleet.js", "utf8"), ctx);
  ctx.fleetRefreshAfter = async () => {};
  return { ctx, calls, toasts, asked };
}
const form = (url, ticked = false) => ({ "#fl_url": { value: url }, "#fl_user": { value: "admin" }, "#fl_pass": { value: "secret" },
  "#fl_own": { value: "https://198.51.100.5:8088" }, "#fl_plain": { checked: ticked } });

test("linking to a plain http:// address waits for the tick that says it goes unencrypted", async () => {
  const t = setup(form("http://192.0.2.250:8088"));
  await t.ctx.fleetLinkSave();
  assert.deepEqual(t.calls, []);
  assert.match(t.toasts[0].m, /unencrypted/);
});

test("with the tick, or over https://, it links", async () => {
  for (const [url, ticked] of [["http://192.0.2.250:8088", true], ["https://192.0.2.250:8443", false]]) {
    const t = setup(form(url, ticked));
    await t.ctx.fleetLinkSave();
    assert.deepEqual(t.calls.map(c => c.path), ["/api/fleet/join"], url);
  }
});

test("a linked cluster or one added before linking at a plain http:// address is marked", () => {
  const t = setup();
  const members = t.ctx.fleetMembersHtml({ linked: true, members: [
    { id: "a", name: "home", self: true, url: "http://192.0.2.10:8088", reachable: true, version: "2" },
    { id: "b", name: "branch", url: "http://192.0.2.250:8088", reachable: true, version: "2" },
    { id: "c", name: "office", url: "https://192.0.2.251:8443", reachable: true, version: "2" }] });
  assert.equal((members.match(/plain HTTP/g) || []).length, 2, "every cluster at http:// is marked, this one too: the others send it the key there");
  assert.match(t.ctx.fleetLegacyHtml([{ name: "old", user: "admin", url: "http://192.0.2.9:8088" }]), /password sent unencrypted/);
  assert.doesNotMatch(t.ctx.fleetLegacyHtml([{ name: "old", user: "admin", url: "https://192.0.2.9:8443" }]), /unencrypted/);
});

test("linking a cluster added before linking, over http://, asks first", async () => {
  const t = setup();
  t.ctx.FLEET.view = { self: "a", linked: true, members: [] };
  t.ctx.FLEET.legacy = [{ name: "old", user: "admin", url: "http://192.0.2.9:8088" }];
  await t.ctx.fleetLinkLegacy("old");
  assert.match(t.asked[0] || "", /unencrypted/);
  assert.deepEqual(t.calls.filter(c => c.path.includes("link")), [], "said no: nothing is sent");
});

test("this cluster's own address at plain http:// also waits for the tick", async () => {
  const fields = form("https://192.0.2.250:8443");
  fields["#fl_own"] = { value: "http://198.51.100.5:8088" };
  const t = setup(fields);
  await t.ctx.fleetLinkSave();
  assert.deepEqual(t.calls, []);
  assert.match(t.toasts[0].m, /unencrypted/);
});

test("using an existing link sends no password, so it does not ask", async () => {
  const t = setup();
  t.ctx.FLEET.view = { self: "a", linked: true, members: [] };
  t.ctx.FLEET.legacy = [{ name: "old", user: "admin", url: "http://192.0.2.9:8088", linked_as: { name: "old" } }];
  await t.ctx.fleetLinkLegacy("old");
  assert.deepEqual(t.asked, []);
  assert.deepEqual(t.calls.map(c => c.path), ["/api/fleet/link-legacy"]);
});
