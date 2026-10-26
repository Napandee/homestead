"use strict";
/* Events › Audit (#738, part 2): the change log and the agent log, read
   together from /api/audit/log, newest first, filtered and paged by the server. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");
const loadUi = require("./helpers/load-ui");

const SOURCE = fs.readFileSync("web/js/views-storage.js", "utf8");

function load({ admin = true } = {}) {
  const calls = [], painted = [];
  const ctx = { console, URLSearchParams, Date, Number, String, Math, JSON, Object, Array, Set,
    STATE: { q: "", data: {} }, can: role => role !== "admin" || admin,
    esc: s => String(s ?? "").replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`),
    jsq: s => `'${String(s).replace(/[\\']/g, "\\$&")}'`,
    fmtAgo: s => `${Math.round(s)}s ago`, fmtAt: t => String(t),
    api: async path => { calls.push(path); return ctx.answer; },
    paint: html => painted.push(html), $: () => null, icon: () => "" };
  ctx.window = ctx;
  vm.createContext(ctx);
  loadUi(ctx);
  // const declarations are not the context's properties: hand the ones tested over.
  vm.runInContext(`${between(SOURCE, "const eventTab = ", "function eventSelect")}
    ${between(SOURCE, "/* ---------------- audit log", "async function viewEvents")}
    Object.assign(globalThis, { eventTab, auditFilter, AUDIT_PAGE });`, ctx);
  return { ctx, calls, painted };
}

test("the Audit tab is offered to admins only, beside Sign-ins", () => {
  const admin = load();
  assert.match(admin.ctx.eventTabs("cluster"), /Sign-ins<\/button>.*Audit<\/button>/s);
  admin.ctx.STATE.eventsTab = "audit";
  assert.equal(admin.ctx.eventTab(), "audit");
  const operator = load({ admin: false });
  operator.ctx.STATE.eventsTab = "audit";
  assert.equal(operator.ctx.eventTab(), "cluster", "a non-admin never lands on it");
  assert.equal(operator.ctx.eventTabs("cluster"), "");
});

test("the filters and the page go to the server", () => {
  const { ctx } = load();
  assert.equal(ctx.auditQuery(), `/api/audit/log?limit=${ctx.AUDIT_PAGE}`);
  Object.assign(ctx.auditFilter(), { source: "agents", person: "robin a", offset: 200 });
  assert.equal(ctx.auditQuery(), `/api/audit/log?limit=${ctx.AUDIT_PAGE}&source=agents&person=robin+a&offset=200`);
});

test("changing a filter starts again at the newest; Older and Newer move a page", async () => {
  const { ctx, calls } = load();
  ctx.answer = { rows: [], offset: 0, limit: ctx.AUDIT_PAGE, more: true, partial: false };
  ctx.STATE.eventsTab = "audit";
  ctx.viewEvents = () => ctx.viewAudit();
  await ctx.auditPage(1);
  assert.equal(ctx.auditFilter().offset, ctx.AUDIT_PAGE);
  await ctx.auditPage(1);
  await ctx.auditPage(-1);
  assert.equal(ctx.auditFilter().offset, ctx.AUDIT_PAGE);
  await ctx.auditPage(-1);
  await ctx.auditPage(-1);
  assert.equal(ctx.auditFilter().offset, 0, "never before the newest");
  ctx.auditFilter().offset = 300;
  await ctx.auditFilterSet("person", " ada ");
  assert.deepEqual([ctx.auditFilter().person, ctx.auditFilter().offset], ["ada", 0]);
  assert.match(calls.at(-1), /person=ada$/);
});

test("a change row says its log, who, what, the target and the result", () => {
  const { ctx } = load();
  const html = ctx.auditRowHtml({ source: "changes", at: Date.now() / 1000 - 60, who: "ada", via: "browser",
    addr: "192.0.2.10", what: "POST /api/workload/scale", target: "lab/Deployment/app-1", result: "ok", code: 200 });
  assert.match(html, /<b>ada<\/b>/);
  assert.match(html, /Change log/);
  assert.match(html, /POST \/api\/workload\/scale/);
  assert.match(html, /lab\/Deployment\/app-1/);
  assert.match(html, /Done/);
  assert.match(html, /192\.0\.2\.10/);
});

test("an agent row names the key, and a refusal or failure is coloured", () => {
  const { ctx } = load();
  const refused = ctx.auditRowHtml({ source: "agents", at: Date.now() / 1000, who: "ada", via: "mcp", addr: "",
    what: "scale_container", key: "Claude Code", result: "refused", detail: "the key lacks containers:manage" });
  assert.match(refused, /Agent log/);
  assert.match(refused, /Claude Code/);
  assert.match(refused, /the key lacks containers:manage/);
  assert.match(refused, /Refused/);
  assert.match(refused, /warn/);
  const failed = ctx.auditRowHtml({ source: "changes", at: 0, who: "", what: "POST /api/x", result: "error", code: 500 });
  assert.match(failed, /Failed/);
  assert.match(failed, /bad/);
  assert.match(failed, /—/, "no one signed in is a dash");
});

test("what a row holds is escaped", () => {
  const { ctx } = load();
  const html = ctx.auditRowHtml({ source: "changes", at: 0, who: "<img src=x>", what: "<script>", result: "ok",
    detail: "\"><b>", target: "<i>" });
  assert.doesNotMatch(html, /<img|<script|<i>|"><b>/);
});

test("the count says which rows these are, and when the search stopped short", () => {
  const { ctx } = load();
  assert.equal(ctx.auditCount({ rows: [{}, {}], offset: 0, more: false, partial: false }), "2 entries from both logs · newest first");
  ctx.auditFilter().source = "agents";
  assert.match(ctx.auditCount({ rows: [{}, {}], offset: 100, more: true, partial: false }), /^101–102 · agent calls/);
  assert.match(ctx.auditCount({ rows: [], offset: 0, more: false, partial: true }), /only the newest lines/);
});

async function demo() {
  const ctx = { URL, URLSearchParams, Response, Date, console, location: { search: "?demo=1", origin: "https://demo.example.com" },
    HOMESTEAD_DEMO: false, document: { body: null, addEventListener() {} }, fetch: async () => { throw new Error("no cluster"); } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/demo.js", "utf8"), ctx);
  return async path => (await ctx.fetch(path)).json();
}

test("the demo answers the audit log, filtered and paged as the server does", async () => {
  const api = await demo();
  const all = await api("/api/audit/log?limit=100");
  assert.ok(all.rows.some(r => r.source === "changes") && all.rows.some(r => r.source === "agents"));
  assert.deepEqual(all.rows.map(r => r.at), [...all.rows.map(r => r.at)].sort((a, b) => b - a), "newest first");
  const agents = await api("/api/audit/log?source=agents");
  assert.ok(agents.rows.length && agents.rows.every(r => r.source === "agents"));
  const alex = await api("/api/audit/log?person=ALEX");
  assert.ok(alex.rows.length && alex.rows.every(r => r.who === "alex"));
  const first = await api("/api/audit/log?limit=2"), second = await api("/api/audit/log?limit=2&offset=2");
  assert.equal(first.rows.length, 2);
  assert.ok(first.more);
  assert.notDeepEqual(first.rows, second.rows);
  assert.equal(second.offset, 2);
});
