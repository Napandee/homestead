"use strict";

/* The Namespaces table says what each one holds, every kind its delete checks,
   and says so when something could not be read instead of calling it empty (#599). */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function load() {
  const ctx = { console, STATE: { data: {} }, document: { addEventListener() {} }, $: () => null, $$: () => [],
    esc: String, jsq: String, jsArg: String, icon: () => "", can: () => true };
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/views-settings.js", "utf8"), ctx);
  return ctx.namespaceHolds;
}

test("every kind the delete checks is named", () => {
  const holds = load();
  assert.equal(holds({ deployments: 2, cronjobs: 1, daemonsets: 1, pods: 3, volumes: 1, vms: 1, disks: 2 }),
    "2 apps · 1 daemon set · 1 scheduled job · 3 running pods · 1 volume · 1 VM · 2 VM disks");
  assert.equal(holds({ deployments: 0, volumes: 0 }), "empty");
});

test("contents that could not be read are said, not shown as empty", () => {
  const holds = load();
  assert.equal(holds({ volumes: 0, unread: ["volumes", "cronjobs"] }), "could not check volumes, cronjobs");
  assert.equal(holds({ deployments: 1, unread: ["pods"] }), "1 app · could not check pods");
});
