// Containers › Schedules: schedules in words, times in the reader's own zone.
"use strict";
process.env.TZ = "Europe/Berlin";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

const core = fs.readFileSync("web/js/core.js", "utf8");

function setup(jobs = []) {
  const out = { calls: [] }, fields = {};
  const field = id => fields[id] ??= { value: "", hidden: false, innerHTML: "", checked: false };
  const ctx = { console, Date, Map, Set, Promise, JSON, document: { addEventListener() {} }, STATE: { data: {} },
    esc: x => String(x ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    paint: html => { out.html = html; }, modal: (title, body) => { out.title = title; out.modal = body; },
    toast: () => {}, closeModal: () => {}, resetPaint: () => {}, ask: async () => true,
    $: field, $$: () => [], icon: () => "",
    api: async (path, opts) => { if (opts) { out.calls.push({ path, body: JSON.parse(opts.body) }); return {}; } return jobs; } };
  ctx.window = ctx;
  vm.createContext(ctx);
  require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(between(core, "const fmtUp", "const upChip"), ctx);
  vm.runInContext(fs.readFileSync("web/js/cron.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/views-lifecycle.js", "utf8"), ctx);
  return { ctx, out, fields: field };
}

const job = extra => ({ name: "nightly-db-dump", schedule: "0 3 * * *", time_zone: "Etc/UTC", image: "postgres:16",
  command: "pg_dumpall", last: "2026-09-22T03:00:04Z", suspend: false, active: 0, ...extra });

test("a schedule is shown in words with its cron in the tooltip", async () => {
  const { ctx, out } = setup([job()]);
  await ctx.viewSchedules();
  assert.match(out.html, /Every day at 03:00 UTC/);
  assert.match(out.html, /data-tip="0 3 \* \* \*"/);
  assert.doesNotMatch(out.html, /<td class="mono">0 3 \* \* \*<\/td>/);
});

test("the last run is in the reader's own time, not the bare UTC string", async () => {
  const { ctx, out } = setup([job()]);
  await ctx.viewSchedules();
  assert.doesNotMatch(out.html, />2026-09-22 03:00:04</);
  assert.match(out.html, /05:00/, "03:00 UTC is 05:00 in Berlin");
  assert.match(out.html, /data-tip="2026-09-22 03:00:04 UTC"/);
});

test("a UTC schedule shows when it runs next", async () => {
  const { ctx, out } = setup([job()]);
  await ctx.viewSchedules();
  assert.match(out.html, /<th[^>]*>Next run<\/th>/);
  assert.match(out.html, /in \d+ (min|h|days)/);
});

test("a job with no zone says it runs on the host's clock and gets no next run", async () => {
  const { ctx, out } = setup([job({ time_zone: "" })]);
  await ctx.viewSchedules();
  assert.match(out.html, /Every day at 03:00 on the host's clock/);
  assert.doesNotMatch(out.html, /03:00 UTC/);
  assert.doesNotMatch(out.html, /in \d+ (min|h|days)/);
});

test("a job in another zone names it and gets no computed next run", async () => {
  const { ctx, out } = setup([job({ time_zone: "America/New_York" })]);
  await ctx.viewSchedules();
  assert.match(out.html, /Every day at 03:00 \(America\/New_York\)/);
  assert.doesNotMatch(out.html, /in \d+ (min|h|days)/);
});

test("the schedule editor offers Data Protection's shapes and saves the cron they build", async () => {
  const { ctx, out, fields } = setup();
  ctx.jobEdit();
  assert.match(out.modal, /id="ls_kind"/);
  assert.match(out.modal, /Every day/);
  Object.assign(fields("#j_name"), { value: "nightly" });
  Object.assign(fields("#j_cmd"), { value: "echo hi" });
  Object.assign(fields("#j_image"), { value: "alpine:3.20" });
  Object.assign(fields("#ls_kind"), { value: "daily" });
  Object.assign(fields("#ls_time"), { value: "04:30" });
  ctx.scheduleChanged();
  assert.match(fields("#ls_next").innerHTML, /Every day at 04:30 UTC\. Next: .*\(your time\)/);
  await ctx.jobSave();
  assert.equal(out.calls[0].body.schedule, "30 4 * * *");
});

test("editing a job with no zone warns that saving pins it to UTC", () => {
  const { ctx, out } = setup();
  ctx.jobEdit(job({ time_zone: "" }));
  assert.match(out.modal, /host's clock/);
  assert.match(out.modal, /pins it to UTC/);
});

test("a CronJob Homestead did not make is read-only and says who manages it", async () => {
  const { ctx, out } = setup([job({ name: "helm-backup", managed: false, managed_by: "Helm" }),
    job({ name: "kubectl-job", managed: false, managed_by: "" })]);
  await ctx.viewSchedules();
  assert.match(out.html, /managed by Helm/);
  assert.match(out.html, /not made here/);
  assert.match(out.html, /Run now/);
  assert.doesNotMatch(out.html, /jobEdit\(\{/);
  assert.doesNotMatch(out.html, /jobDel\(/);
});

test("a schedule Homestead made can still be edited and deleted", async () => {
  const { ctx, out } = setup([job({ managed: true, managed_by: "" })]);
  await ctx.viewSchedules();
  assert.match(out.html, /jobEdit\(\{/);
  assert.match(out.html, /jobDel\(/);
  assert.doesNotMatch(out.html, /managed by|not made here/);
});

test("saving sends the generation the page read, and an empty one for a new schedule", async () => {
  const { ctx, out, fields } = setup();
  ctx.jobEdit(job({ managed: true, generation: 41 }));
  Object.assign(fields("#j_name"), { value: "nightly-db-dump" });
  Object.assign(fields("#j_cmd"), { value: "pg_dumpall" });
  await ctx.jobSave();
  assert.equal(out.calls[0].body.generation, 41);
  ctx.jobEdit();
  Object.assign(fields("#j_name"), { value: "fresh" });
  await ctx.jobSave();
  assert.equal(out.calls[1].body.generation, "");
});
