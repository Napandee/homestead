"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

test("finished imports offer dismissal while active and recovery records keep their history", () => {
  const host = {}, actions = [], opened = [], dismissed = [];
  const ctx = { STATE: { data: { operations: [
    { id: "failed-copy", kind: "unraid-vm-import", title: "Import desktop", status: "failed", progress: 100, message: "Upload refused" },
    { id: "running-copy", kind: "unraid-vm-import", title: "Import server", status: "running", progress: 10 },
    { id: "protected-copy", kind: "unraid-vm-import", title: "Inspect partial copy", status: "failed", dismissible: false },
    { id: "finished-copy", kind: "unraid-vm-import", title: "Import completed", status: "succeeded" },
    { id: "cancelled-copy", kind: "unraid-vm-import", title: "Import cancelled", status: "cancelled" },
    { id: "other", kind: "deployment", status: "running" },
  ] } }, $: () => host, esc: String, jsq: JSON.stringify, operationTone: () => "",
    operationActive: op => !["succeeded", "failed", "cancelled"].includes(op.status),
    actionBar: rows => { actions.push(...rows); return ""; }, operationLog: id => opened.push(id),
    dismissOperation: id => dismissed.push(id) };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/views-vms.js", "utf8"), ctx);
  ctx.uvmCopiesPaint();
  assert.match(host.innerHTML, /Recent imports/);
  assert.match(host.innerHTML, /Upload refused/);
  assert.equal(actions.length, 8);
  assert.equal(actions[0].label, "Job details");
  vm.runInContext(actions[0].run, ctx);
  assert.deepEqual(opened, ["failed-copy"]);
  for (const action of actions.filter(row => row.label === "Dismiss")) {
    assert.equal(action.need, "operator");
    vm.runInContext(action.run, ctx);
  }
  assert.deepEqual(dismissed, ["failed-copy", "finished-copy", "cancelled-copy"]);
});

test("the shared dismissal refreshes the import list only after the server accepts it", async () => {
  const host = {}, requests = [], errors = [], summary = {};
  const ctx = { STATE: { data: { operations: [
    { id: "failed-copy", kind: "unraid-vm-import", title: "Import desktop", status: "failed" },
    { id: "running-copy", kind: "unraid-vm-import", title: "Import server", status: "running" },
  ] } }, $: selector => selector === "#jobSummary" ? summary : host, esc: String, jsq: JSON.stringify,
    actionBar: () => "", toast: message => errors.push(message),
    api: async (path, init) => { requests.push({ path, body: JSON.parse(init.body) }); } };
  ctx.window = ctx;
  vm.createContext(ctx);
  for (const file of ["operations", "views-vms"]) vm.runInContext(fs.readFileSync(`web/js/${file}.js`, "utf8"), ctx);
  vm.runInContext("renderOperations = () => uvmCopiesPaint()", ctx);
  await ctx.dismissOperation("failed-copy");
  assert.deepEqual(requests, [{ path: "/api/operations/dismiss", body: { id: "failed-copy" } }]);
  assert.equal(ctx.STATE.data.operations.length, 1);
  assert.equal(ctx.STATE.data.operations[0].id, "running-copy");
  assert.doesNotMatch(host.innerHTML, /Import desktop/);
  assert.match(host.innerHTML, /Import server/);
  ctx.api = async () => { throw new Error("An active operation cannot be dismissed"); };
  await ctx.dismissOperation("running-copy");
  assert.equal(ctx.STATE.data.operations[0].id, "running-copy");
  assert.deepEqual(errors, ["An active operation cannot be dismissed"]);
});

/* A Proxmox host's VMs (#proxmox import): picked by VMID, worded as Proxmox. */
function importPage(kind) {
  const hosts = {}, actions = [], posted = [];
  const pve = { id: "100", name: "win11-gaming", state: "stopped", shut_off: true, ready: true, os: "Windows 11",
    cores: 4, memory: "8Gi", firmware: "uefi", disks: [{ index: 0, path: "/dev/pve/vm-100-disk-1", size_gb: 128, format: "raw" }] };
  const running = { ...pve, id: "101", name: "web", state: "running", shut_off: false };
  const ctx = { STATE: { data: { srcs: [{ name: "pve", kind, user: "root", host: "192.0.2.10", ssh_trust: { key: "x" } }], uvms: {} } },
    document: { getElementById: id => hosts[id] ||= {} }, esc: String, jsq: JSON.stringify,
    actionBar: rows => { actions.push(...rows.filter(Boolean)); return rows.filter(Boolean).map(r => r.label).join("|"); },
    api: async (path, init) => { posted.push([path, JSON.parse(init.body)]); return { source: "pve", kind, virsh: true, vms: [pve, running] }; } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/views-vms.js", "utf8"), ctx);
  return { ctx, hosts, actions, posted };
}

test("a Proxmox host's VMs are listed with their VMID and picked by it", async () => {
  const { ctx, hosts, actions } = importPage("proxmox");
  await ctx.uvmLoad("pve");
  const html = hosts["uvm-pve"].innerHTML;
  assert.match(html, /win11-gaming<\/b> <span class="dim xs mono">100<\/span>/);
  const imp = actions.find(a => a.label === "Import"), off = actions.find(a => a.label === "Shut down");
  assert.equal(imp.run, 'uvmImport("pve","100")', "picked by VMID, not by name");
  assert.equal(off.run, 'uvmShutdown("pve","101",this)');
  assert.match(off.tip, /on Proxmox/);
});

test("an Unraid server's VMs keep their name as their id and say Unraid", async () => {
  const { ctx, hosts, actions } = importPage("unraid");
  await ctx.uvmLoad("pve");
  assert.doesNotMatch(hosts["uvm-pve"].innerHTML, /dim xs mono">100/);
  assert.match(actions.find(a => a.label === "Shut down").tip, /on Unraid/);
});

test("every import server on the VM page can be removed", () => {
  const src = fs.readFileSync("web/js/views-vms.js", "utf8");
  const page = src.slice(src.indexOf("async function viewVmImport"), src.indexOf("window.viewVmImport"));
  assert.match(page, /label: "Remove server", icon: "trash", run: `srcDel\(\$\{jsq\(s\.name\)\}\)`, need: "admin", danger: true/);
  assert.doesNotMatch(page, /kind !== "proxmox"/, "Proxmox hosts are listed too");
});
