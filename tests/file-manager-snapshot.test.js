"use strict";

/* A snapshot opens in the file manager as a read-only pane beside the live
   volume it was taken from, and restores into it after a review. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

const STATE = { namespace: "lab", session: "homestead-snapshot-files-" + "a".repeat(24), expires: 1700001800,
  source: { claim: "data", snapshot: "daily", created: "2026-10-18T06:00:00Z" } };
const PLAN = { ready: true, blockers: [], adds: ["conf.d/new.conf"],
  replaces: [{ path: "app.conf", live: { size: 7, modified: 200 }, snapshot: { size: 10, modified: 100 } }],
  skipped: [{ path: "root", why: "it is a link to somewhere outside the volume" }], in_use: ["grafana"],
  warning: "grafana is running and using this volume; it sees these files change while it runs. For a database, stop it first.",
  session_ends_at: 1700001800, snapshot: "daily", review_token: "0123456789abcdef0123" };

function element() {
  return { innerHTML: "", textContent: "", hidden: false, value: "", dataset: {},
    classList: { toggle() {} }, setAttribute() {}, addEventListener() {}, querySelector: () => null };
}

async function open({ restore } = {}) {
  const calls = [], nodes = new Map();
  const $ = key => { if (!nodes.has(key)) nodes.set(key, element()); return nodes.get(key); };
  const ctx = { console, Date, Promise, setTimeout, clearTimeout, setInterval, clearInterval, Set, Map, URLSearchParams, JSON,
    document: { addEventListener() {}, removeEventListener() {}, createElement: () => ({ click() {}, remove() {} }), body: { appendChild() {} } },
    STATE: { data: {} }, $, $$: () => [],
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"),
    icon: () => "", fileSize: n => `${n} B`, fmtAt: v => `at ${v}`, fmtClock: () => "now",
    jsArg: s => JSON.stringify(String(s ?? "")), modal() {}, closeModal() {}, toast() {}, ask: async () => true,
    api: async (path, options = {}) => {
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ path, body });
      if (path === "/api/volumes") return [];
      if (path.startsWith("/api/snapshot-files/list")) return { path: "", read_only: true, entries: [
        { name: "app.conf", kind: "file", size: 10, mode: "644", uid: 0, gid: 0, modified: 100, editable: false },
        { name: "conf.d", kind: "dir", size: 0, mode: "755", uid: 0, gid: 0, modified: 100, editable: false }] };
      if (path.startsWith("/api/files/entries")) return { path: "", entries: [
        { name: "app.conf", kind: "file", size: 7, mode: "644", uid: 0, gid: 0, user: "root", group: "root", modified: 200, editable: true }] };
      if (path === "/api/snapshot-files/restore/plan") return JSON.parse(JSON.stringify(PLAN));
      if (path === "/api/snapshot-files/restore") return restore ? restore() : { ok: true, job: { id: "j1", status: "done", bytes: 17 } };
      if (path.startsWith("/api/files/transfer")) return { id: "j1", status: "done", bytes: 17 };
      return { ok: true, stopped: true };
    } };
  ctx.window = ctx; vm.createContext(ctx);
  ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  for (const file of ["web/js/ui.js", "web/js/filemanager.js"]) vm.runInContext(fs.readFileSync(file, "utf8"), ctx);
  await ctx.fileManagerSnapshot(JSON.parse(JSON.stringify(STATE)));
  return { ctx, calls, html: key => $(key).innerHTML, FM: vm.runInContext("FM", ctx) };
}

test("the snapshot pane is titled with the snapshot and when it was taken, and offers no write actions", async () => {
  const { ctx, html, FM } = await open();
  assert.equal(FM.panes[0].snapshot.session, STATE.session);
  assert.match(html("#fm_pane0"), /Snapshot · daily · at 2026-10-18T06:00:00Z/);
  assert.match(html("#fm_pane0"), /read-only/);
  for (const absent of ["New folder", "Upload files", "<select"]) assert.doesNotMatch(html("#fm_pane0"), new RegExp(absent));
  ctx.fmClick({}, 0, "app.conf");
  const actions = html("#fm_actions");
  for (const absent of ["Edit", "Rename", "Permissions", "Delete", "Move", "Copy"]) assert.doesNotMatch(actions, new RegExp(`>${absent}`));
  assert.match(actions, /Download/);
  assert.match(actions, /<button[^>]*fmRestoreReview\(\)[^>]*data-need="admin"[^>]*>Restore to data<\/button>/);
});

test("the live pane stays writable and switchable, and offers no copy into the snapshot", async () => {
  const { ctx, html } = await open();
  assert.match(html("#fm_pane1"), /New folder/);
  assert.match(html("#fm_pane1"), /<select aria-label="Volume in this pane"/);
  ctx.fmClick({}, 1, "app.conf");
  assert.match(html("#fm_actions"), />Delete/);
  assert.doesNotMatch(html("#fm_actions"), /Copy →|Move →/);
});

test("the snapshot lists through its session and downloads through it", async () => {
  const { ctx, calls } = await open();
  const list = calls.find(c => c.path.startsWith("/api/snapshot-files/list"));
  assert.equal(new URLSearchParams(list.path.split("?")[1]).get("session"), STATE.session);
  let href = "";
  ctx.document.createElement = () => ({ set href(v) { href = v; }, click() {}, remove() {} });
  ctx.fmClick({}, 0, "conf.d");
  ctx.fmDownload();
  assert.match(href, /^\/api\/snapshot-files\/download\?/);
  assert.equal(new URLSearchParams(href.split("?")[1]).get("path"), "conf.d");
});

test("Restore reviews what it adds and replaces, with both sizes and the in-use warning", async () => {
  const { ctx, calls, html } = await open();
  ctx.fmClick({}, 0, "app.conf");
  await ctx.fmRestoreReview();
  const plan = calls.find(c => c.path === "/api/snapshot-files/restore/plan");
  assert.deepEqual(plan.body, { namespace: "lab", session: STATE.session, folder: "", paths: ["app.conf"],
    target: { namespace: "lab", pvc: "data", folder: "" } });
  const review = html("#fm_panel");
  assert.match(review, /app\.conf/);
  assert.match(review, /7 B/);
  assert.match(review, /10 B/);
  assert.match(review, /conf\.d\/new\.conf/);
  assert.match(review, /outside the volume/);
  assert.match(review, /grafana is running and using this volume/);
  assert.match(review, /<button[^>]*fmRestoreRun\(\)[^>]*data-need="admin"/);
});

test("the run posts the reviewed token and follows the job", async () => {
  const { ctx, calls } = await open();
  ctx.fmClick({}, 0, "app.conf");
  await ctx.fmRestoreReview();
  await ctx.fmRestoreRun();
  const run = calls.find(c => c.path === "/api/snapshot-files/restore");
  assert.equal(run.body.review_token, PLAN.review_token);
  assert.deepEqual(run.body.paths, ["app.conf"]);
});

test("a changed review answers 409 and offers Review again with the fresh plan", async () => {
  const fresh = { ...PLAN, review_token: "fffffffffffffffffff0",
    replaces: [{ path: "app.conf", live: { size: 9, modified: 300 }, snapshot: { size: 10, modified: 100 } }] };
  const { ctx, html } = await open({ restore: () => { throw Object.assign(new Error("what the restore would change has changed; review it again"), { status: 409, body: { error: "what the restore would change has changed; review it again", plan: fresh } }); } });
  ctx.fmClick({}, 0, "app.conf");
  await ctx.fmRestoreReview();
  await ctx.fmRestoreRun();
  assert.match(html("#fm_panel"), /changed; review it again/);
  assert.match(html("#fm_panel"), /9 B/);
  assert.match(html("#fm_panel"), />Review again</);
  assert.doesNotMatch(html("#fm_panel"), /fmRestoreRun\(\)/);
});

test("closing the manager ends the snapshot session and stops only the live helper", async () => {
  const { ctx, calls } = await open();
  await ctx.fmClose();
  const closes = calls.filter(c => /close$/.test(c.path));
  assert.deepEqual(closes.map(c => c.path).sort(), ["/api/files/close", "/api/snapshot-files/close"]);
  assert.deepEqual(closes.find(c => c.path === "/api/snapshot-files/close").body, { namespace: "lab", session: STATE.session });
  assert.equal(closes.find(c => c.path === "/api/files/close").body.pvc, "data");
});
