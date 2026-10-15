"use strict";

/* Port, uplink and address state is said in words as well as colour, so it
   reads without telling red from green and can be reached by keyboard. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function load(...files) {
  const ctx = { console, Map, Set, Date, Promise, JSON, document: { addEventListener() {} }, STATE: { data: {}, platform: {} },
    $: () => null, $$: () => [], icon: () => "", can: () => true, tip: (text, label = "?") => `<span class="tip" tabindex="0">${label}</span>`,
    esc: v => String(v ?? "").replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`) };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  for (const file of files) vm.runInContext(fs.readFileSync(file, "utf8"), ctx);
  return ctx;
}
const nic = (name, extra = {}) => ({ name, kind: "nic", link: "up", speed_mbps: 2500, master: "", ...extra });

test("each host's ports say how many are up and down, and each light names its port", () => {
  const ctx = load("web/js/host-ports.js");
  const html = vm.runInContext("portsAcrossHostsBody", ctx)({ hosts: [{ node: "host-1", available: true, uplink: "eth0",
    ports: [nic("eth0"), nic("eth1"), nic("eth2", { link: "down", speed_mbps: null })], conditions: [] }], conditions: [] });
  assert.match(html, /2 up · 1 down/);
  assert.match(html, /class="port-led down"[^>]*role="img"[^>]*aria-label="eth2: down"/);
  assert.match(html, /class="port-led up"[^>]*role="img"[^>]*aria-label="eth0: up, 2.5 Gb\/s"/);
});

test("an uplink that is not ready on a host says so beside its name", () => {
  const ctx = load("web/js/host-ports.js");
  const html = vm.runInContext("uplinkHostChips", ctx)({ nodes: ["host-1", "host-2", "host-3"],
    status: { "host-1": { ready: true }, "host-2": { ready: false, message: "bond0 has no link" } } });
  assert.match(html, /host-1<\/span>/);
  assert.match(html, /host-2 · not ready/);
  assert.match(html, /host-3 · not reported yet/);
  assert.match(html, /aria-label="host-2: bond0 has no link"/);
});

test("an address's warning and note flags can be reached and say what they are", () => {
  const ctx = load("web/js/views-ipam.js");
  const html = vm.runInContext("ipamRow", ctx)({ ip: "192.0.2.10", cluster: "", name: "printer", flags: [
    { level: "warn", text: "Two devices answer on this address" }, { level: "info", text: "Seen by UniFi only" }] });
  assert.match(html, /class="ipam-flag warn"[^>]*tabindex="0"[^>]*aria-label="Warning: Two devices answer on this address"/);
  assert.match(html, /class="ipam-flag info"[^>]*tabindex="0"[^>]*aria-label="Note: Seen by UniFi only"/);
});
