"use strict";
// Colour means state (docs/design.md): green healthy, amber needs a person,
// red failed, blue opens something. Progress and labels stay neutral, so a
// healthy page is calm (#490).
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs");
const read = f => fs.readFileSync(f, "utf8");
// A top-level `const NAME = {...};` object literal, evaluated.
const map = (file, name) => {
  const src = read(file), from = src.indexOf(`const ${name} = {`);
  assert.ok(from >= 0, `${name} in ${file}`);
  return Function(`return (${src.slice(src.indexOf("{", from), src.indexOf("};", from) + 1)})`)();
};

test("Image Cache: only active is green; rollback, stopped and scheduled are neutral", () => {
  const reasons = map("web/js/views-lifecycle.js", "IMAGE_REASONS");
  assert.equal(reasons.active.tone, "ok");
  for (const r of ["rollback", "stopped", "scheduled"]) assert.equal(reasons[r].tone, "", r);
});

test("Schedules and Import show a running or waiting job as neutral, not amber", () => {
  const src = read("web/js/views-lifecycle.js");
  assert.doesNotMatch(src, /<span class="pill med">running<\/span>/);
  assert.doesNotMatch(src, /j\.state === "failed" \? "crit" : "med"/);
});

test("App Store and Helm labels are neutral: guidance, official, verified, installed here", () => {
  const store = read("web/js/views-workloads.js");
  assert.doesNotMatch(store, /level === "dependency" \? "warn" : "info"/);
  const helm = read("web/js/views-helm.js");
  assert.doesNotMatch(helm, /class="tag (ok|info)">(official|verified)</);
  for (const [, [, tone]] of Object.entries(map("web/js/views-helm.js", "HELM_MANAGED"))) assert.equal(tone, "");
});

test("a VM in progress or paused is neutral; Running is green", () => {
  const tones = map("web/js/views-vms.js", "VM_TONE");
  assert.equal(tones.Running, "ok");
  for (const s of ["Stopped", "Paused", "Migrating", "Starting", "Stopping", "Deleting", "Provisioning", "WaitingForVolumeBinding"])
    assert.equal(tones[s], "low", s);
});

test("a k3s VM's cluster and role tags are the shared neutral cluster tag", () => {
  const src = read("web/js/views-vms.js");
  assert.doesNotMatch(src, /vmClusterTag = v => v\.cluster \? `<span class="tag info"/);
  assert.doesNotMatch(src, /<span class="tag info">server<\/span>/);
});

test("an Unraid VM that is shut off has a pill, as Stopped does", () => {
  assert.match(read("web/js/views-vms.js"), /<span class="pill \$\{off \? "low" : "ok"\}">/);
});

test("Dashboard widgets: low severity and acknowledged findings are neutral, running jobs too", () => {
  const src = read("web/js/insights.js");
  assert.match(src, /\{critical:"bad",medium:"warn",low:"",healthy:"ok"\}\[severity\] \?\? ""/);
  assert.doesNotMatch(src, /tone:acked\?"info"/);
});

test("the bell: a waiting agent request is amber, alerts take their worst severity", () => {
  const src = read("web/js/homestead-update.js");
  assert.match(src, /AI agent request"\)\} waiting for you`, "warn"\)/);
  assert.doesNotMatch(src, /plural\(alertCount, "active alert"\), "danger"\)/);
});

test("AI agent requests: pending waits for a person, so amber; running and denied are neutral", () => {
  const status = map("web/js/views-agents.js", "AGENT_STATUS");
  assert.equal(status.pending[1], "warn");
  assert.equal(status.running[1], "");
  assert.equal(status.denied[1], "");
  const outcome = map("web/js/views-agents.js", "AGENT_OUTCOME");
  assert.equal(outcome.pending, "warn");
  assert.equal(outcome.denied, "");
  assert.equal(outcome.refused, "warn");
});
