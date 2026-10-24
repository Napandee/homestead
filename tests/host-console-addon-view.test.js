"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function setup(admin = true, confirm = true) {
  const calls = [], toasts = [];
  const ctx = { console, STATE: { data: {} }, $: () => null, $$: () => [], esc: String, can: () => admin,
    ask: async () => confirm, toast: (m, k) => toasts.push([m, k]), addonsPaint: () => {},
    api: async (path, opts) => { calls.push({ path, body: opts ? JSON.parse(opts.body) : null }); return { detail: "being installed" }; } };
  ctx.window = ctx; ctx.jsq = JSON.stringify; vm.createContext(ctx);
  require("./helpers/load-ui")(ctx);
  vm.runInContext(fs.readFileSync("web/js/platform.js", "utf8"), ctx);
  ctx.addonsPaint = () => {};
  return { ctx, calls, toasts };
}

const state = (over = {}) => ({ version: "2.8.247", enabled: true, hosts: 2, installed: 2, current: 1, settled: false, harvester: false,
  nodes: [{ name: "node1", ready: true, enabled: true, detail: "Installed 2.8.246; update available" },
          { name: "node2", ready: true, enabled: true, current: true, detail: "Installed 2.8.247; matches this release" },
          { name: "native", ready: true, native: true }], ...over });

test("the host console is one add-on row: its state, each host, one Enable or Disable button", () => {
  const { ctx } = setup();
  const html = ctx.hostConsoleRow(state());
  assert.match(html, /Host console/);
  assert.match(html, /1 of 2 up to date/);
  assert.match(html, /node1.*update available/s);
  assert.doesNotMatch(html, /native/, "Harvester's own console is not a host to manage");
  assert.match(html, /onclick="[^"]*hostConsoleSet\(false\)">Disable</, "on, it offers Disable");
  assert.doesNotMatch(html, /type="checkbox"/, "it acts at once, so it is a button, not a switch");
  assert.match(ctx.hostConsoleRow(state({ enabled: false, installed: 0 })), /hostConsoleSet\(true\)">Enable</);
  assert.match(ctx.hostConsoleRow(state({ settled: true, current: 2 })), /ui-chip ok">installed/);
  assert.match(ctx.hostConsoleRow(state({ enabled: false, installed: 1 })), /removing · 1 left/);
  assert.equal(ctx.hostConsoleRow(state({ harvester: true })), "");
  assert.equal(ctx.hostConsoleRow(null), "", "not an administrator, or not readable: no row");
});

test("Enable or Disable sets the add-on for every host, and asks before removing it", async () => {
  const on = setup();
  await on.ctx.hostConsoleSet(true);
  assert.deepEqual(on.calls[0], { path: "/api/host-console", body: { enabled: true } });
  const declined = setup(true, false);
  await declined.ctx.hostConsoleSet(false);
  assert.equal(declined.calls.length, 0, "declined: nothing is sent");
  const off = setup();
  await off.ctx.hostConsoleSet(false);
  assert.deepEqual(off.calls[0].body, { enabled: false });
});

test("a viewer sees the row without the button", () => {
  const { ctx } = setup(false);
  assert.doesNotMatch(ctx.hostConsoleRow(state()), /hostConsoleSet/);
});
