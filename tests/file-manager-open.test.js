"use strict";

/* Opening the file browser on a volume an app is using. The helper runs on
   the node the volume is attached to, so it opens beside the app; only a
   ReadWriteOncePod claim, which one pod alone may mount, still needs the
   app stopped. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function open(...args) {
  const asked = [], body = { innerHTML: "" };
  const ctx = { console, Date, Promise, setTimeout, clearTimeout, setInterval, clearInterval, Set, Map,
    document: { addEventListener() {}, removeEventListener() {} }, STATE: { data: {} },
    $: key => key === "#mbody" ? body : null, $$: () => [], esc: String, icon: () => "",
    modal() {}, closeModal() {}, toast() {},
    ask: async (message, options = {}) => { asked.push({ message, options }); return false; },
    api: () => new Promise(() => {}) };
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/filemanager.js", "utf8"), ctx);
  return ctx.fileManager(...args).then(() => asked);
}

test("a shared volume an app is using opens without asking", async () => {
  assert.deepEqual(await open("lab", "media", true, ["ReadWriteMany"]), []);
});

test("a ReadWriteOnce volume an app is using says its live files are edited, not that the app must stop", async () => {
  const [prompt] = await open("lab", "frigate-config", true, ["ReadWriteOnce"]);
  assert.ok(prompt, "it asks first");
  assert.doesNotMatch(prompt.message, /once the workload is stopped/);
  assert.match(prompt.message, /live files/);
  assert.equal(prompt.options.ok, "Open files");
});

test("a ReadWriteOncePod volume an app is using still says the app must stop first", async () => {
  const [prompt] = await open("lab", "db", true, ["ReadWriteOncePod"]);
  assert.match(prompt.message, /stop/i);
});

test("a volume nothing is using opens without asking", async () => {
  assert.deepEqual(await open("lab", "scratch", false, ["ReadWriteOnce"]), []);
});
