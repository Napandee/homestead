"use strict";

/* VM reviews read like a container start: one sentence, one notice when
   there is something to accept, a short acknowledgement that matches the
   action, and a button that names the result. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const core = fs.readFileSync("web/js/core.js", "utf8");

function context() {
  const c = { document: { addEventListener() {}, querySelector() { return null; } }, FLEET: { view: {} }, icon: () => "",
    appAvatar: () => "", clusterAttr: () => "", clusterTag: () => "", remoteRow: () => false, platformTag: () => "", hardwareTags: () => "",
    fmtUp: String, svcUrl: () => "", fmtAgo: String, ratePair: () => ["0", "Mb/s"], sizePair: () => "", sizeText: () => "", meter: () => "", sev: () => "" };
  c.window = c; vm.createContext(c);
  vm.runInContext(core.slice(0, core.indexOf("/* Put text on the clipboard.")), c);
  for (const file of ["metrics-utils.js", "ui.js", "uptime.js", "autoupdate.js", "schedules.js", "views-workloads.js"])
    vm.runInContext(fs.readFileSync(`web/js/${file}`, "utf8"), c);
  return c;
}
const plan = extra => ({ blocked: false, pod_request_gb: 2, pod_cpu_request_percent: 10, additional: 1, candidates: [],
  placement: {}, blockers: [], concerns: [], unbounded: ["home-assistant-os"], ...extra });

test("the acknowledgement names the action it accepts", () => {
  const c = context();
  assert.equal(c.reviewAck("Restarts"), "Restart it anyway");
  assert.equal(c.reviewAck("Resumes"), "Resume it anyway");
  assert.equal(c.reviewAck("Starts 3 pods of"), "Start it anyway");
  const html = c.startReview(plan(), { what: "Restarts", name: "home-assistant-os", ackId: "ok" });
  assert.match(html, /Restart it anyway/);
  assert.doesNotMatch(html, /Start it anyway/);
});

test("a schedule set in another time zone says so in its time line, not as a second notice", () => {
  const c = context(); let html = "";
  c.modal = (title, body) => { html = body; }; c.api = () => new Promise(() => {}); c.schedPreview = () => {};
  c.schedRow = () => ({ schedule: { tz: "Pacific/Chatham", days: [0, 1], start: "07:00", stop: "23:00" } });
  const zone = vm.runInContext("schedZone()", c);
  c.scheduleDialog("vm", "default", "home-assistant-os");
  assert.doesNotMatch(html, /ui-callout/);
  assert.ok(html.includes(`Times in ${zone}. These times were set in Pacific/Chatham; saving sets them in ${zone}.`), html);
});
