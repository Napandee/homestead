"use strict";
// Start on another host (#441): the review dialog and its run.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

const PLAN = { ready: true, blockers: [], node: "node2", down_since: "2026-10-17T10:00:00Z", down_seconds: 1500,
  last_heard: "2026-10-17T09:59:40Z", set: { ns: "data", name: "postgres", wait_mode: false },
  pods: [{ name: "postgres-0", uid: "u0", terminating: false }, { name: "postgres-1", uid: "u1", terminating: true }],
  volumes: [{ pvc: "data-postgres-0", volume: "pvc-aaa", copy_on: "node1" }], review_token: "tok1" };

function setup({ plan = PLAN, post = null } = {}) {
  const fields = { "#mbody": { innerHTML: "" }, "#mtitle": { textContent: "" }, "#sr_confirm": { value: "postgres", dataset: { typedExpect: "postgres" } },
    "#sr_run": { disabled: false, textContent: "Start on another host", onclick: null } };
  const calls = [], toasts = [], requests = [];
  const ctx = { console, Map, Set, Promise, JSON, URLSearchParams, setTimeout, encodeURIComponent, document: { addEventListener() {} },
    STATE: { data: {} }, $: key => fields[key], $$: () => [], can: () => true, tip: t => t,
    toast: (m, k) => toasts.push([m, k]),
    esc: value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    jsArg: value => JSON.stringify(String(value)),
    modal: (title, html) => { fields["#mbody"].innerHTML = html; fields["#mtitle"].textContent = title; },
    closeModal: () => calls.push(["closeModal"]), refresh: () => calls.push(["refresh"]),
    noteOperation: op => calls.push(["noteOperation", op.id]), refreshOperations: () => {},
    api: async (url, opts) => {
      requests.push([url, opts]);
      if (!opts) { if (plan instanceof Error) throw plan; return plan; }
      if (post instanceof Error) throw post;
      return post;
    } };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(fs.readFileSync("web/js/set-restart.js", "utf8"), ctx);
  return { ctx, fields, calls, toasts, requests, html: () => fields["#mbody"].innerHTML };
}

test("the review shows each check as a fact, every pod and volume, and the typed confirmation", async () => {
  const t = setup();
  await t.ctx.setRestartReview("data", "postgres", "node2");
  const html = t.html();
  assert.match(html, /Not ready since/);
  assert.match(html, /25 min/);
  assert.match(html, /Last heard from/);
  assert.match(html, /postgres-0/); assert.match(html, /postgres-1/);
  assert.match(html, /already being deleted/);
  assert.match(html, /data-postgres-0/); assert.match(html, /current copy is on[^]*node1/);
  assert.match(html, /data-typed-expect="postgres"/);
  assert.match(html, /id="sr_run"[^>]*disabled/);
  assert.match(html, /data-need="admin"/);
  assert.match(html, /stuck pod is removed; the StatefulSet starts it again on another host/i);
  assert.match(html, /about 6 minutes/);
  assert.doesNotMatch(html, /overrides that once/);
  assert.match(t.requests[0][0], /^\/api\/statefulset\/restart-elsewhere\/plan\?/);
  assert.match(t.requests[0][0], /ns=data&name=postgres&node=node2/);
});

test("a set that waits by its own setting says this overrides that once", async () => {
  const t = setup({ plan: { ...PLAN, set: { ...PLAN.set, wait_mode: true } } });
  await t.ctx.setRestartReview("data", "postgres", "node2");
  assert.match(t.html(), /this app is set to wait for its host to come back; this overrides that once/i);
});

test("a blocked plan shows the blockers in a bad callout and no run button", async () => {
  const t = setup({ plan: { ready: false, blockers: ["Longhorn still sees node2 as up", "<b>x</b>"] } });
  await t.ctx.setRestartReview("data", "postgres", "node2");
  const html = t.html();
  assert.match(html, /ui-callout bad/);
  assert.match(html, /Longhorn still sees node2 as up/);
  assert.doesNotMatch(html, /<b>x<\/b>/);
  assert.doesNotMatch(html, /sr_run/);
  assert.doesNotMatch(html, /data-typed-expect/);
});

test("a plan that cannot be read toasts and opens nothing", async () => {
  const t = setup({ plan: new Error("boom") });
  await t.ctx.setRestartReview("data", "postgres", "node2");
  assert.equal(t.toasts[0][1], "bad"); assert.equal(t.html(), "");
});

test("running posts the typed name and the review token, then follows the job", async () => {
  const t = setup({ post: { id: "op1", not_deleted: [{ name: "postgres-1", error: "gone" }] } });
  await t.ctx.setRestartReview("data", "postgres", "node2");
  await t.ctx.setRestartRun("data", "postgres", "node2");
  const body = JSON.parse(t.requests[1][1].body);
  assert.deepEqual(body, { ns: "data", name: "postgres", node: "node2", confirm: "postgres", review_token: "tok1" });
  assert.deepEqual(t.calls.find(c => c[0] === "noteOperation"), ["noteOperation", "op1"]);
  assert.ok(t.toasts.some(([m]) => /postgres-1/.test(m) && /gone/.test(m)), "names the pod not deleted");
});

test("nothing is sent when the typed name does not match", async () => {
  const t = setup({ post: { id: "op1" } });
  await t.ctx.setRestartReview("data", "postgres", "node2");
  t.fields["#sr_confirm"].value = "nope";
  await t.ctx.setRestartRun("data", "postgres", "node2");
  assert.equal(t.requests.length, 1);
});

test("a 409 turns the button into Review again, which reopens the review", async () => {
  const err = Object.assign(new Error("The review changed"), { status: 409, body: { error: "The review changed", plan: PLAN } });
  const t = setup({ post: err });
  await t.ctx.setRestartReview("data", "postgres", "node2");
  await t.ctx.setRestartRun("data", "postgres", "node2");
  assert.equal(t.fields["#sr_run"].textContent, "Review again");
  assert.equal(t.fields["#sr_run"].disabled, false);
  t.fields["#sr_run"].onclick();
  await new Promise(r => setTimeout(r, 0));
  assert.equal(t.requests.filter(([, o]) => !o).length, 2);
});

test("a 409 that names a running operation follows it instead", async () => {
  const err = Object.assign(new Error("already running"), { status: 409, body: { error: "already running", operation: { id: "op9" } } });
  const t = setup({ post: err });
  await t.ctx.setRestartReview("data", "postgres", "node2");
  await t.ctx.setRestartRun("data", "postgres", "node2");
  assert.ok(t.calls.some(c => c[0] === "noteOperation" && c[1] === "op9"));
});
