"use strict";
// #927: the Housekeeping protection plan makes no Longhorn trim job (one in
// the default group switches Homestead's own weekly trim off, and fails each
// week on detached volumes and VM disks), and Data protection says when
// Longhorn's recurring jobs could not be read instead of showing none.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function view() {
  const ctx = { console, Promise, JSON, URLSearchParams, location: { search: "", href: "http://example.com/" },
    STATE: { data: {}, q: "", view: "protect" }, document: { addEventListener() {}, getElementById: () => null },
    $: () => null, moreMenu: () => "", meter: () => "", icon: () => "", tip: t => t, nextRun: () => "",
    fmtAt: String, fmtWhen: String, CRON: { describe: String },
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;") };
  ctx.jsq = s => ctx.esc(JSON.stringify(s)); ctx.window = ctx;
  vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(fs.readFileSync("web/js/views-protect.js", "utf8"), ctx);
  return ctx;
}

test("no protection plan makes a Longhorn trim job; Housekeeping keeps the snapshot clean-up and says Homestead trims", () => {
  const ctx = view();
  const plans = vm.runInContext("PROTECT_PLANS", ctx);
  for (const plan of plans) assert.ok(!plan.jobs.some(j => j.task === "filesystem-trim"), `${plan.id} makes no trim job`);
  const tidy = plans.find(p => p.id === "tidy");
  assert.equal(tidy.jobs.map(j => j.task).join(), "snapshot-cleanup");
  assert.match(tidy.blurb, /Homestead trims every attached volume itself/);
});
