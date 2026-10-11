"use strict";
// Add a host: the role chosen decides the command shown.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

const GUIDE = { distribution: "k3s", server: "192.0.2.5", version: "1.31.4+k3s1", token_file: "/var/lib/rancher/k3s/server/node-token",
  agent: "AGENT-CMD", server_join: "SERVER-CMD", tiebreaker: "TIEBREAKER-CMD", settings_known: true,
  shared_flags: ["--cluster-cidr", "--default-local-storage-path"], longhorn: "LONGHORN-CMD" };

function setup(guide = GUIDE) {
  const fields = { "#mbody": { innerHTML: "" } };
  const ctx = { console, Promise, document: { addEventListener() {} }, STATE: { data: {} },
    $: key => fields[key] || (fields[key] = { innerHTML: "" }), $$: () => [],
    esc: value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    modal: () => {}, guideCopy: cmd => `<pre>${cmd}</pre>`, api: async () => guide };
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/platform.js", "utf8"), ctx);
  return { ctx, fields };
}

test("the guide opens on an app host when no role is asked for", async () => {
  const t = setup();
  await t.ctx.platformJoinGuide();
  assert.match(t.fields["#mbody"].innerHTML, /value="app" checked/);
  assert.match(t.fields["#mbody"].innerHTML, /AGENT-CMD/);
});

test("a role asked for is chosen and its command shown", async () => {
  const t = setup();
  await t.ctx.platformJoinGuide("tiebreaker");
  assert.match(t.fields["#mbody"].innerHTML, /value="tiebreaker" checked/);
  assert.match(t.fields["#mbody"].innerHTML, /TIEBREAKER-CMD/);
  assert.doesNotMatch(t.fields["#mbody"].innerHTML, /AGENT-CMD/);
});

test("choosing another role redraws only the command", async () => {
  const t = setup();
  await t.ctx.platformJoinGuide();
  t.ctx.platformJoinRole("control-plane");
  assert.match(t.fields["#joinCmd"].innerHTML, /SERVER-CMD/);
});

test("without the servers' settings the guide names the flags to copy", () => {
  const html = setup().ctx.joinCommandHtml({ ...GUIDE, settings_known: false }, "tiebreaker");
  assert.match(html, /--default-local-storage-path/);
  assert.match(html, /copy/i);
});

test("the roles use the shared answer-mode choice, not unstyled markup", async () => {
  const t = setup();
  await t.ctx.platformJoinGuide();
  assert.match(t.fields["#mbody"].innerHTML, /<div class="answer-modes">[^]*<label class="answer-mode"><input type="radio" name="joinRole" value="app" checked/);
});

test("on RKE2 a server or tiebreaker joins through the installer, never as an agent", () => {
  const rke2 = { distribution: "rke2", agent: "RKE2-AGENT-CMD", config: "server: x", check: "CHECK-CMD" };
  const html = setup().ctx.joinCommandHtml(rke2, "tiebreaker");
  assert.doesNotMatch(html, /RKE2-AGENT-CMD/);
  assert.match(html, /HS_ROLE=tiebreaker/);
  assert.match(setup().ctx.joinCommandHtml(rke2, "control-plane"), /HS_ROLE=server/);
  assert.match(setup().ctx.joinCommandHtml(rke2, "app"), /RKE2-AGENT-CMD/);
});

test("a server or tiebreaker is checked before it joins", () => {
  const html = setup().ctx.joinCommandHtml({ ...GUIDE, check: "CHECK-CMD" }, "tiebreaker");
  assert.match(html, /CHECK-CMD[^]*TIEBREAKER-CMD/);
  assert.doesNotMatch(setup().ctx.joinCommandHtml({ ...GUIDE, check: "CHECK-CMD" }, "app"), /CHECK-CMD/);
});
