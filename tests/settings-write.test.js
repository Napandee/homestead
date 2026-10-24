"use strict";

/* Every settings save is the settings as they are now, read fresh, with only
   that save's change: never a copy kept since the page opened, and never the
   defaults that stand in for a failed read (#601). */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

const STORED = { site_name: "Main site", catalog_url: "https://example.com/feed.json",
  thresholds: { cpu: { warning: 70, critical: 88 } }, updates: { policy: "maintenance_window", maintenance: { start: "03:00" } },
  placement: { rebalance: "suggest", mode: "standby", standby_nodes: ["host-3"] }, mcp: { enabled: false }, info: { version: "x" } };

// stale: how many saves the server refuses as based on changed settings (409).
function load({ readFails = false, revision, stale = 0 } = {}) {
  const posts = [];
  let reads = 0;
  const ctx = { console, Promise, JSON, STATE: { data: {} }, document: { addEventListener() {}, getElementById: () => null },
    $: () => null, $$: () => [], esc: String, jsq: String, jsArg: String, icon: () => "", can: () => true,
    toast() {}, closeModal() {}, viewSettings() {}, loadImageUpdates() {}, mcpCardPaint() {},
    ask: async () => true, HEALTH_DEFAULTS: { thresholds: {} }, HEALTH: {},
    loadHealthSettings: async () => ctx.STATE.data.appSettings,
    api: async (path, options = {}) => {
      if (options.method === "POST") {
        const body = JSON.parse(options.body); posts.push(body);
        if (stale-- > 0) throw Object.assign(new Error("The settings were changed since they were read here"), { status: 409 });
        return body;
      }
      if (path === "/api/settings") {
        if (readFails) throw new Error("Bad Gateway");
        reads++;
        return JSON.parse(JSON.stringify({ ...STORED, site_name: reads > 1 ? "Renamed" : STORED.site_name, revision: revision && `${revision}.${reads}` }));
      }
      return {};
    } };
  ctx.window = ctx; vm.createContext(ctx);
  for (const f of ["ui", "views-settings", "views-agents"]) vm.runInContext(fs.readFileSync(`web/js/${f}.js`, "utf8"), ctx);
  // What the page kept when it opened: since then someone changed placement.
  ctx.STATE.data.appSettings = { thresholds: {}, updates: {}, placement: { mode: "spread" } };
  return { ctx, posts };
}

const without = (doc, ...keys) => { const out = { ...doc }; keys.forEach(k => delete out[k]); return out; };

test("a save sends the settings as they are now, with only its change", async () => {
  const { ctx, posts } = load();
  await ctx.settingsWrite({ catalog_url: "" });
  assert.deepEqual(posts, [{ ...without(STORED, "info"), catalog_url: "" }]);
});

test("a save names the revision it read, so the server can refuse it if the settings changed since", async () => {
  const { ctx, posts } = load({ revision: "41" });
  await ctx.settingsWrite(current => ({ revision: "1", site_name: current.site_name + " 2" }));
  assert.equal(posts[0].revision, "41.1");
  assert.equal(posts[0].site_name, "Main site 2");
});

test("a save refused as stale is tried once more on a fresh read, with the change made to that", async () => {
  const { ctx, posts } = load({ revision: "41", stale: 1 });
  const saved = await ctx.settingsWrite(current => ({ site_name: current.site_name + " 2" }));
  assert.equal(posts.length, 2);
  assert.deepEqual([posts[1].revision, posts[1].site_name], ["41.2", "Renamed 2"]);
  assert.equal(saved.site_name, "Renamed 2");
});

test("a save refused as stale twice gives up with the server's words", async () => {
  const { ctx, posts } = load({ revision: "41", stale: 2 });
  await assert.rejects(ctx.settingsWrite({ catalog_url: "" }), /changed since they were read here/);
  assert.equal(posts.length, 2);
});

test("the catalogue dialog closes only when the save was made", async () => {
  for (const [stale, closes] of [[2, 0], [0, 1]]) {
    const { ctx } = load({ stale });
    let closed = 0;
    ctx.closeModal = () => closed++;
    ctx.viewSettings = () => {};
    ctx.$ = id => ({ "#set_catalog": { value: "https://example.com/feed.json" } })[id] || null;
    await ctx.catalogSave(false);
    assert.equal(closed, closes, `after ${stale} refusals`);
  }
});

test("a save whose read fails saves nothing", async () => {
  const { ctx, posts } = load({ readFails: true });
  await assert.rejects(ctx.settingsWrite({ catalog_url: "" }), /could not be read, so nothing was saved/);
  assert.deepEqual(posts, []);
});

test("turning AI agents on changes nothing else", async () => {
  const { ctx, posts } = load();
  await ctx.mcpSet(true);
  assert.deepEqual(posts, [{ ...without(STORED, "info"), mcp: { enabled: true } }]);
});

test("the update window keeps the rest of the update policy as it is now", async () => {
  const { ctx, posts } = load();
  ctx.$ = id => ({ "#set_update_start": { value: "02:00" }, "#set_update_duration": { value: "120" } })[id] || null;
  await ctx.updateWindowSave();
  assert.equal(posts[0].updates.policy, "maintenance_window");
  assert.equal(posts[0].updates.maintenance.start, "02:00");
  assert.deepEqual(posts[0].placement, STORED.placement);
});

test("the old partial saves are gone", () => {
  const { ctx } = load();
  for (const name of ["saveHealthSettings", "saveUpdateSettings", "saveSiteName"]) assert.equal(typeof ctx[name], "undefined", name);
});

test("turning metrics on asks first, since every API key can then read them; declined, nothing is saved", async () => {
  for (const [answer, saved] of [[false, []], [true, [{ ...without(STORED, "info"), metrics: { enabled: true } }]]]) {
    const { ctx, posts } = load();
    const asked = [];
    ctx.ask = async (text, options) => { asked.push([text, options]); return answer; };
    ctx.selfHealthPaint = () => {};
    await ctx.metricsSet(true);
    assert.equal(asked.length, 1);
    assert.match(asked[0][0], /every API key/i);
    assert.equal(asked[0][1].ok, "Turn on metrics");
    assert.deepEqual(posts, saved);
  }
});

test("turning metrics off acts at once: it only narrows who can read", async () => {
  const { ctx, posts } = load();
  let asked = 0;
  ctx.ask = async () => { asked++; return false; };
  ctx.selfHealthPaint = () => {};
  await ctx.metricsSet(false);
  assert.equal(asked, 0);
  assert.deepEqual(posts, [{ ...without(STORED, "info"), metrics: { enabled: false } }]);
});

// The save bar's form: every section drawn, as the page opened with it.
function form(ctx, values) {
  const fields = { "#set_cpu_warn": { value: "60" }, "#set_cpu_crit": { value: "90" }, "#set_memory_warn": { value: "80" },
    "#set_memory_crit": { value: "95" }, "#set_disk_warn": { value: "80" }, "#set_disk_crit": { value: "90" },
    "#set_temperature_warn": { value: "70" }, "#set_temperature_crit": { value: "85" },
    "#set_site_name": { value: "Old name" }, "#set_rebalance": { value: "off" }, "#set_place_mode": { value: "spread" },
    "#set_update_policy": { value: "approval_required" }, "#set_notify_available": { checked: true }, "#set_notify_failures": { checked: true },
    ...values };
  ctx.$ = id => fields[id] || null;
}

test("the save bar sends only the sections whose fields were changed", async () => {
  const { ctx, posts } = load();
  form(ctx, { "#set_cpu_warn": { value: "65" } });
  ctx.STATE.settingsDirty = new Set(["app:set_cpu_warn"]);
  await ctx.settingsSave();
  assert.equal(posts.length, 1);
  assert.equal(posts[0].thresholds.cpu.warning, 65);
  assert.equal(posts[0].site_name, "Main site", "the name on the form was not touched");
  assert.deepEqual(posts[0].placement, STORED.placement, "nor was placement");
  assert.deepEqual(posts[0].updates, STORED.updates, "nor the update policy");
});

test("when the settings could not be read when the page opened, the save bar saves nothing", async () => {
  const { ctx, posts } = load();
  const toasts = [];
  ctx.toast = (m, k) => toasts.push({ m, k });
  form(ctx, { "#set_cpu_warn": { value: "65" } });
  ctx.STATE.data.appSettings = ctx.HEALTH_DEFAULTS;   // what a failed read leaves
  ctx.STATE.settingsDirty = new Set(["app:set_cpu_warn"]);
  await ctx.settingsSave();
  assert.deepEqual(posts, []);
  assert.match(toasts.at(-1)?.m || "", /could not be read/);
});
