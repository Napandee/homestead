"use strict";
// Data Protection's dialogs take the shared parts (#501): a lead, facts, at
// most one notice, explanations in UI.more, and a title that names its object.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const src = fs.readFileSync("web/js/views-protect.js", "utf8");

const PLAN = { backup: "backup-7f3a91", source_volume: "pvc-demo-frigate", minimum_size_gb: 20,
  suggested_name: "frigate-config-restore", storage_class: "longhorn-r2", storage_classes: ["longhorn-r2"] };
async function restore(label) {
  const titles = [], body = { innerHTML: "" };
  const ctx = { document: { addEventListener() {} }, STATE: { data: {} }, setTimeout: () => 0, clearTimeout() {},
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    modal: title => titles.push(title), $: id => (id === "#mbody" ? body : id === "#mtitle" ? { set textContent(t) { titles.push(t); } } : null),
    tip: t => `<span class="tip" data-tip="${t}">?</span>`, icon: () => "", jsArg: s => JSON.stringify(String(s)),
    api: async path => (path.startsWith("/api/namespaces") ? ["lab"] : PLAN), defaultNamespace: () => "lab" };
  ctx.jsq = s => ctx.esc(ctx.jsArg(s)); ctx.window = ctx;
  vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(src, ctx);
  await ctx.lhRestore(PLAN.backup, label);
  return { html: body.innerHTML, titles };
}

test("Restore backup names what it restores, from the Backups row", async () => {
  const { titles } = await restore("frigate-config");
  assert.ok(titles.every(t => t === "Restore backup · frigate-config"), titles.join(" | "));
});

test("Restore backup opens with a lead and lists the backup as facts, with no notice boxes", async () => {
  const { html } = await restore("frigate-config");
  assert.match(html, /^\s*<p class="ui-lead">/);
  assert.match(html, /<dl class="ui-facts">[\s\S]*Backup[\s\S]*Source volume[\s\S]*Original capacity/);
  assert.doesNotMatch(html, /class="drow"/);
  assert.doesNotMatch(html, /class="note/);
});

test("the destination name is checked at its field, not in a box", async () => {
  const { html } = await restore("frigate-config");
  assert.match(html, /<input id="lr_name"[^>]*>[\s\S]{0,200}id="lr_check"/);
  assert.match(html, /id="lr_check" class="ui-help"/);
  const check = src.slice(src.indexOf("window.lhRestoreCheck"), src.indexOf("\n};\n", src.indexOf("window.lhRestoreCheck")));
  assert.doesNotMatch(check, /note (bad|good)/);
  assert.match(check, /ui-field-error/);
});
