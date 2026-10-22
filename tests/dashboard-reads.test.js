// The Dashboard's 15 s refresh rereads only what changes (#705): the saved layout
// is read once and kept until it is saved or five minutes pass, and the long
// history is read again only when its range changes or five minutes pass.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

const answer = {revision: "r1", layout: {version: 1, items: [{id: "compute", width: 4, height: 0}]}};

function dashboardWithReads() {
  const reads = [];
  global.ME = "alice";
  global.window = global.window || {};
  delete global.document;  // the insights tests below leave a stand-in that dashboard.js must not see
  global.api = async (path) => { reads.push(path); return JSON.parse(JSON.stringify(answer)); };
  global.localStorage = {getItem: () => null, setItem() {}, removeItem() {}};
  delete require.cache[require.resolve("../web/js/dashboard.js")];
  return {dashboard: require("../web/js/dashboard.js"), reads};
}

test("the saved layout is read once, not on every refresh", async () => {
  const {dashboard, reads} = dashboardWithReads();
  for (let i = 0; i < 4; i++) assert.equal(await dashboard.load(), true);
  assert.deepEqual(reads, ["/api/auth/preferences/dashboard"]);
});

test("the layout is read again after five minutes", async (t) => {
  const {dashboard, reads} = dashboardWithReads();
  let now = 1_000_000;
  t.mock.method(Date, "now", () => now);
  await dashboard.load();
  now += 4 * 60_000;
  await dashboard.load();
  assert.equal(reads.length, 1);
  now += 2 * 60_000;
  await dashboard.load();
  assert.equal(reads.length, 2);
});

test("another account reads its own layout", async () => {
  const {dashboard, reads} = dashboardWithReads();
  await dashboard.load();
  global.ME = "bob";
  await dashboard.load();
  assert.equal(reads.length, 2);
});

test("a layout that failed to load is tried again on the next refresh", async () => {
  const {dashboard, reads} = dashboardWithReads();
  let fail = true;
  global.api = async (path) => { reads.push(path); if (fail) throw new Error("timed out"); return JSON.parse(JSON.stringify(answer)); };
  assert.equal(await dashboard.load(), false);
  fail = false;
  assert.equal(await dashboard.load(), true);
  assert.equal(reads.length, 2);
});

test("invalidate forgets the layout so the next refresh reads it", async () => {
  const {dashboard, reads} = dashboardWithReads();
  await dashboard.load();
  dashboard.invalidate();
  await dashboard.load();
  assert.equal(reads.length, 2);
});

// The long history card (views-stats.js), loaded with just what historyPaint needs.
function historyWithReads() {
  const reads = [];
  const host = {innerHTML: "", querySelector: () => null, appendChild() {}, replaceWith() {}};
  const context = vm.createContext({
    console, setTimeout, clearTimeout, AbortController, Date, Math, JSON, Promise, window: {},
    STATE: {data: {}}, range: "24h",
    $: (sel) => sel === "#historyCard" ? host : null,
    api: async (path) => { reads.push(path); return {samples: 0, t: [], cpu: [], mem: [], rx: [], tx: [], pods: [], nodes: [], vol_bad: []}; },
    esc: String, fmtDay: String, jsq: JSON.stringify,
    document: {createElement: () => ({innerHTML: "", className: "", setAttribute() {}})},
    morph: () => {}, historyStat: () => "", dualSpark: () => "",
  });
  const source = fs.readFileSync(require.resolve("../web/js/views-stats.js"), "utf8");
  vm.runInContext(source + "\n;this.historyPaint = historyPaint; historyRange = () => range;", context);
  return {context, reads};
}

test("the long history is not read again on every refresh", async () => {
  const {context, reads} = historyWithReads();
  for (let i = 0; i < 3; i++) await context.historyPaint();
  assert.equal(reads.length, 1);
});

test("the long history is read again when its range changes", async () => {
  const {context, reads} = historyWithReads();
  await context.historyPaint();
  context.range = "7d";
  await context.historyPaint();
  assert.deepEqual(reads.map(p => p.split("=")[1]), ["24h", "7d"]);
});

// Health insights (insights.js): the overview the page just read is reused, not read again.
function insightsWithReads(file = "../web/js/insights.js") {
  const reads = [];
  const el = {dataset: {insight: "health"}, cloneNode: () => ({innerHTML: ""})};
  global.window = {};
  global.STATE = {platform: {}};
  global.document = {querySelectorAll: (sel) => sel === "[data-insight]" ? [el] : [], querySelector: () => null};
  global.morph = () => {};
  global.UI = new Proxy({}, {get: () => () => ""});
  global.esc = String; global.jsArg = JSON.stringify; global.jsq = JSON.stringify;
  global.fmtAge = String; global.fmtDay = String; global.toast = () => {};
  global.api = async (path) => { reads.push(path); return path === "/api/overview" ? {health_issues: []} : {}; };
  delete require.cache[require.resolve(file)];
  return {insights: require(file), reads};
}

test("health insights reuse the overview the page just read", async () => {
  const {insights, reads} = insightsWithReads(process.env.INSIGHTS_FILE || "../web/js/insights.js");
  insights.seed?.("overview", {health_issues: []});
  await insights.load(true);
  assert.equal(reads.filter(p => p === "/api/overview").length, 0);
  assert.ok(reads.includes("/api/alerts"), "what the page did not read is still read");
});

test("without a fresh overview from the page, health insights read it themselves", async () => {
  const {insights, reads} = insightsWithReads(process.env.INSIGHTS_FILE || "../web/js/insights.js");
  await insights.load(true);
  assert.equal(reads.filter(p => p === "/api/overview").length, 1);
});

test("opening the editor reads the latest layout, even when one is kept", async () => {
  const {dashboard, reads} = dashboardWithReads();
  await dashboard.load();
  answer.revision = "r2";  // another device saved meanwhile
  try {
    assert.equal(await dashboard.load({fresh: true}), true);
    assert.equal(reads.length, 2);
  } finally { answer.revision = "r1"; }
});
